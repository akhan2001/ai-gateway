"""GET /api/v1/forecast — projected spend.

The projection is deliberately simple and stated as such: month-to-date spend
run-rated to the full month, and month-over-month growth compounded forward.
It is a trend line, not a model, and the response says so via `method` so the
dashboard never implies more precision than the data supports.
"""

from __future__ import annotations

from calendar import monthrange
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends

from ..auth import current_workspace
from ..db import db, f

router = APIRouter(prefix="/api/v1", tags=["forecast"])

# What a realistic optimization pass recovers: moving the worst-offending
# workloads onto market-rate models. Applied to measured overpayment only, so
# a workspace already at or below market shows no phantom savings.
_RECOVERABLE_SHARE_OF_OVERPAY = 0.7


@router.get("/forecast")
async def forecast(workspace_id: UUID = Depends(current_workspace)) -> dict:
    monthly = await db.fetch(
        """
        SELECT date_trunc('month', "timestamp") AS month,
               SUM(cost_usd)       AS cost_usd,
               SUM(overpay_usd)    AS overpay_usd
        FROM usage_records
        WHERE workspace_id = $1 AND priced
          AND "timestamp" >= date_trunc('month', NOW()) - INTERVAL '3 months'
        GROUP BY month
        ORDER BY month
        """,
        workspace_id,
    )

    now = datetime.now(timezone.utc)
    days_in_month = monthrange(now.year, now.month)[1]
    day_of_month = now.day

    this_month = 0.0
    this_month_overpay = 0.0
    prior: list[float] = []
    for row in monthly:
        cost = f(row["cost_usd"])
        if row["month"].month == now.month and row["month"].year == now.year:
            this_month = cost
            this_month_overpay = f(row["overpay_usd"])
        else:
            prior.append(cost)

    # Run-rate the partial month.
    projected_month = (this_month / day_of_month) * days_in_month if day_of_month else 0.0

    last_month = prior[-1] if prior else 0.0
    growth_rate = ((projected_month - last_month) / last_month) if last_month else 0.0
    # Clamp: a workspace's first partial month can produce absurd growth that
    # would compound into a meaningless annual figure.
    growth_rate = max(-0.9, min(growth_rate, 1.0))

    months_left = 12 - now.month
    annual = projected_month
    running = projected_month
    for _ in range(months_left):
        running *= 1 + growth_rate
        annual += running

    overpay_rate = (this_month_overpay / this_month) if this_month else 0.0
    optimized_annual = annual * (1 - overpay_rate * _RECOVERABLE_SHARE_OF_OVERPAY)

    return {
        "method": "month-to-date run rate, MoM growth compounded forward",
        "current_month_to_date_usd": round(this_month, 4),
        "projected_this_month_usd": round(projected_month, 4),
        "last_month_usd": round(last_month, 4),
        "mom_growth_pct": round(growth_rate * 100, 2),
        "projected_rest_of_year_usd": round(annual, 2),
        "projected_with_optimization_usd": round(optimized_annual, 2),
        "potential_saving_usd": round(annual - optimized_annual, 2),
        "months_of_history": len(monthly),
        "low_confidence": len(monthly) < 2 or day_of_month < 3,
    }
