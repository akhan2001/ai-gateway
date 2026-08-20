"""GET /api/v1/summary — the four dashboard stat cards."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends

from ..auth import current_workspace
from ..db import db, f

router = APIRouter(prefix="/api/v1", tags=["summary"])


@router.get("/summary")
async def summary(workspace_id: UUID = Depends(current_workspace)) -> dict:
    # Calendar months, not rolling 30-day windows — the card says "this month",
    # and a rolling window would disagree with the customer's invoice.
    totals = await db.fetchrow(
        """
        SELECT
            COALESCE(SUM(cost_usd)       FILTER (WHERE "timestamp" >= date_trunc('month', NOW())), 0) AS this_month,
            COALESCE(SUM(acpi_bench_usd) FILTER (WHERE "timestamp" >= date_trunc('month', NOW())), 0) AS this_month_bench,
            COALESCE(SUM(cost_usd)       FILTER (WHERE "timestamp" >= date_trunc('month', NOW()) - INTERVAL '1 month'
                                                   AND "timestamp" <  date_trunc('month', NOW())), 0) AS last_month,
            COUNT(*) FILTER (WHERE "timestamp" >= date_trunc('month', NOW()))                          AS requests
        FROM usage_records
        WHERE workspace_id = $1 AND priced
        """,
        workspace_id,
    )
    totals = totals or {}

    top = await db.fetchrow(
        """
        SELECT model_id, SUM(cost_usd) AS cost_usd
        FROM usage_records
        WHERE workspace_id = $1 AND priced AND "timestamp" >= date_trunc('month', NOW())
        GROUP BY model_id
        ORDER BY cost_usd DESC
        LIMIT 1
        """,
        workspace_id,
    )

    this_month = f(totals.get("this_month"))
    last_month = f(totals.get("last_month"))
    bench = f(totals.get("this_month_bench"))

    return {
        "this_month_spend_usd": round(this_month, 4),
        "last_month_spend_usd": round(last_month, 4),
        "mom_change_pct": round(((this_month - last_month) / last_month) * 100, 2)
        if last_month
        else None,
        "acpi_benchmark_usd": round(bench, 4),
        "vs_acpi_pct": round(((this_month - bench) / bench) * 100, 2) if bench else None,
        "top_model": (
            {"model_id": top["model_id"], "cost_usd": round(f(top["cost_usd"]), 4)}
            if top
            else None
        ),
        "total_requests": int(totals.get("requests") or 0),
    }
