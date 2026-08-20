"""GET /api/v1/usage — spend time series, and GET /api/v1/models."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from ..auth import current_workspace
from ..db import db, f

router = APIRouter(prefix="/api/v1", tags=["usage"])

# Whitelist, not interpolation: `group_by` reaches SQL as an identifier, so it
# can never be taken from user input directly.
_GROUPINGS = {
    "model": "model_id",
    "provider": "provider",
    "feature": "feature_tag",
    "workload": "workload_tag",
    "day": None,
}


@router.get("/usage")
async def usage(
    days: int = Query(30, ge=1, le=365),
    group_by: str = Query("model"),
    workspace_id: UUID = Depends(current_workspace),
) -> dict:
    if group_by not in _GROUPINGS:
        raise HTTPException(
            status_code=400,
            detail=f"group_by must be one of {sorted(_GROUPINGS)}",
        )

    column = _GROUPINGS[group_by]

    if column is None:
        rows = await db.fetch(
            """
            SELECT time_bucket(INTERVAL '1 day', "timestamp") AS day,
                   COUNT(*)            AS requests,
                   SUM(input_tokens)   AS input_tokens,
                   SUM(output_tokens)  AS output_tokens,
                   SUM(cost_usd)       AS cost_usd,
                   SUM(acpi_bench_usd) AS acpi_bench_usd,
                   SUM(overpay_usd)    AS overpay_usd
            FROM usage_records
            WHERE workspace_id = $1 AND priced
              AND "timestamp" >= NOW() - make_interval(days => $2)
            GROUP BY day
            ORDER BY day
            """,
            workspace_id,
            days,
        )
        series = [
            {
                "day": row["day"].date().isoformat(),
                "requests": int(row["requests"]),
                "input_tokens": int(row["input_tokens"] or 0),
                "output_tokens": int(row["output_tokens"] or 0),
                "cost_usd": round(f(row["cost_usd"]), 6),
                "acpi_bench_usd": round(f(row["acpi_bench_usd"]), 6),
                "overpay_usd": round(f(row["overpay_usd"]), 6),
            }
            for row in rows
        ]
        return {"days": days, "group_by": group_by, "series": series}

    rows = await db.fetch(
        f"""
        SELECT COALESCE({column}, '(untagged)') AS bucket,
               COUNT(*)            AS requests,
               SUM(input_tokens)   AS input_tokens,
               SUM(output_tokens)  AS output_tokens,
               SUM(cost_usd)       AS cost_usd,
               SUM(acpi_bench_usd) AS acpi_bench_usd,
               SUM(overpay_usd)    AS overpay_usd
        FROM usage_records
        WHERE workspace_id = $1 AND priced
          AND "timestamp" >= NOW() - make_interval(days => $2)
        GROUP BY bucket
        ORDER BY cost_usd DESC
        """,
        workspace_id,
        days,
    )
    return {
        "days": days,
        "group_by": group_by,
        "buckets": [
            {
                "key": row["bucket"],
                "requests": int(row["requests"]),
                "input_tokens": int(row["input_tokens"] or 0),
                "output_tokens": int(row["output_tokens"] or 0),
                "cost_usd": round(f(row["cost_usd"]), 6),
                "acpi_bench_usd": round(f(row["acpi_bench_usd"]), 6),
                "overpay_usd": round(f(row["overpay_usd"]), 6),
            }
            for row in rows
        ],
    }


@router.get("/models")
async def models(
    days: int = Query(30, ge=1, le=365),
    workspace_id: UUID = Depends(current_workspace),
) -> dict:
    rows = await db.fetch(
        """
        SELECT model_id,
               provider,
               COUNT(*)                 AS requests,
               SUM(input_tokens)        AS input_tokens,
               SUM(output_tokens)       AS output_tokens,
               SUM(cost_usd)            AS cost_usd,
               SUM(acpi_bench_usd)      AS acpi_bench_usd,
               SUM(overpay_usd)         AS overpay_usd,
               -- Score is a per-model constant, so any row's value will do;
               -- MAX just picks one deterministically.
               MAX(acpi_score)          AS acpi_score
        FROM usage_records
        WHERE workspace_id = $1 AND priced
          AND "timestamp" >= NOW() - make_interval(days => $2)
        GROUP BY model_id, provider
        ORDER BY cost_usd DESC
        """,
        workspace_id,
        days,
    )
    return {
        "days": days,
        "models": [
            {
                "model_id": row["model_id"],
                "provider": row["provider"],
                "requests": int(row["requests"]),
                "input_tokens": int(row["input_tokens"] or 0),
                "output_tokens": int(row["output_tokens"] or 0),
                "cost_usd": round(f(row["cost_usd"]), 6),
                "acpi_bench_usd": round(f(row["acpi_bench_usd"]), 6),
                "overpay_usd": round(f(row["overpay_usd"]), 6),
                "acpi_score": f(row["acpi_score"]) or None,
            }
            for row in rows
        ],
    }
