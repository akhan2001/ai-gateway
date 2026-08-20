"""GET /api/v1/benchmark — actual spend vs the ACPI market rate."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query

from ..auth import current_workspace
from ..db import db, f

router = APIRouter(prefix="/api/v1", tags=["benchmark"])

# Below this, a percentage swing is noise rather than a finding worth showing.
_MIN_SPEND_FOR_OPPORTUNITY_USD = 1.0


@router.get("/benchmark")
async def benchmark(
    days: int = Query(30, ge=1, le=365),
    workspace_id: UUID = Depends(current_workspace),
) -> dict:
    rows = await db.fetch(
        """
        SELECT model_id,
               provider,
               SUM(cost_usd)       AS cost_usd,
               SUM(acpi_bench_usd) AS acpi_bench_usd,
               SUM(overpay_usd)    AS overpay_usd,
               SUM(input_tokens + output_tokens) AS tokens,
               MAX(acpi_score)     AS acpi_score
        FROM usage_records
        WHERE workspace_id = $1 AND priced
          AND "timestamp" >= NOW() - make_interval(days => $2)
        GROUP BY model_id, provider
        ORDER BY cost_usd DESC
        """,
        workspace_id,
        days,
    )

    models = []
    total_cost = 0.0
    total_bench = 0.0

    for row in rows:
        cost = f(row["cost_usd"])
        bench = f(row["acpi_bench_usd"])
        overpay = f(row["overpay_usd"])
        total_cost += cost
        total_bench += bench

        pct = (overpay / bench * 100) if bench else None
        models.append(
            {
                "model_id": row["model_id"],
                "provider": row["provider"],
                "tokens": int(row["tokens"] or 0),
                "cost_usd": round(cost, 6),
                "acpi_bench_usd": round(bench, 6),
                "overpay_usd": round(overpay, 6),
                "overpay_pct": round(pct, 2) if pct is not None else None,
                "acpi_score": f(row["acpi_score"]) or None,
                "status": (
                    "above_market"
                    if pct is not None and pct > 0
                    else "below_market"
                    if pct is not None
                    else "unknown"
                ),
            }
        )

    opportunities = [
        {
            "model_id": m["model_id"],
            "action": (
                f"{m['model_id']} costs {m['overpay_pct']:.0f}% above the ACPI market rate. "
                f"Moving this workload to a cheaper model at similar quality would recover "
                f"about ${m['overpay_usd']:.2f} over the last {days} days."
            ),
            "potential_saving_usd": round(m["overpay_usd"], 2),
        }
        for m in models
        if m["overpay_usd"] > _MIN_SPEND_FOR_OPPORTUNITY_USD and m["overpay_pct"]
    ][:3]

    return {
        "days": days,
        "total_cost_usd": round(total_cost, 4),
        "acpi_benchmark_usd": round(total_bench, 4),
        "overpay_usd": round(total_cost - total_bench, 4),
        "overpay_pct": round(((total_cost - total_bench) / total_bench) * 100, 2)
        if total_bench
        else None,
        "models": models,
        "opportunities": opportunities,
    }
