"""GET/POST /api/v1/budget — a workspace's monthly spend limit and its status.

One budget per workspace. The status numbers here (current spend, projected
end-of-month) use the same month-to-date run-rate as `forecast.py`, so the
dashboard's budget line and its forecast chart never disagree about what
"this month" has cost so far.
"""

from __future__ import annotations

from calendar import monthrange
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ..auth import current_workspace
from ..db import db, f

router = APIRouter(prefix="/api/v1", tags=["budget"])


@router.get("/budget")
async def get_budget(workspace_id: UUID = Depends(current_workspace)) -> dict:
    budget = await db.fetchrow(
        "SELECT monthly_limit_usd, alert_pct, alert_email FROM budgets WHERE workspace_id = $1",
        workspace_id,
    )
    if budget is None:
        return {"configured": False}

    row = await db.fetchrow(
        """
        SELECT COALESCE(SUM(cost_usd), 0) AS spend
        FROM usage_records
        WHERE workspace_id = $1 AND priced
          AND "timestamp" >= date_trunc('month', NOW())
        """,
        workspace_id,
    )
    current_spend = f(row["spend"] if row else 0)

    now = datetime.now(timezone.utc)
    days_in_month = monthrange(now.year, now.month)[1]
    day_of_month = now.day

    projected_eom = (current_spend / day_of_month) * days_in_month if day_of_month else 0.0
    limit = f(budget["monthly_limit_usd"])

    return {
        "configured": True,
        "monthly_limit_usd": round(limit, 2),
        "alert_pct": budget["alert_pct"],
        "alert_email": budget["alert_email"],
        "current_spend_usd": round(current_spend, 4),
        "pct_used": round((current_spend / limit) * 100, 2) if limit else 0.0,
        "projected_eom_usd": round(projected_eom, 2),
        "will_exceed": projected_eom > limit,
        "days_remaining": days_in_month - day_of_month,
    }


class BudgetRequest(BaseModel):
    monthly_limit_usd: float = Field(gt=0)
    alert_pct: int = Field(default=80, gt=0, le=100)
    alert_email: str


@router.post("/budget")
async def set_budget(
    body: BudgetRequest, workspace_id: UUID = Depends(current_workspace)
) -> dict:
    await db.fetchrow(
        """
        INSERT INTO budgets (workspace_id, monthly_limit_usd, alert_pct, alert_email)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (workspace_id) DO UPDATE
            SET monthly_limit_usd = EXCLUDED.monthly_limit_usd,
                alert_pct         = EXCLUDED.alert_pct,
                alert_email       = EXCLUDED.alert_email,
                updated_at        = NOW()
        RETURNING id
        """,
        workspace_id,
        body.monthly_limit_usd,
        body.alert_pct,
        body.alert_email,
    )
    return {"success": True}
