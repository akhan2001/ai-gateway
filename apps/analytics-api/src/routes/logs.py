"""GET /api/v1/logs, GET /api/v1/logs/summary — the raw per-request ledger.

Unlike /api/v1/usage and /api/v1/models, this is not an aggregate: one row in
the response is one row of usage_records, unpriced requests included (an
unrecognised model_id should still show up here, it just carries priced=false
instead of a cost). Cursor-paginated on (timestamp, request_id) rather than
OFFSET, since usage_records is a TimescaleDB hypertable that keeps growing —
OFFSET pagination gets slower (and can skip/repeat rows under concurrent
writes) the deeper a customer pages, a cursor on an indexed column does not.
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from ..auth import current_workspace
from ..db import db, f

router = APIRouter(prefix="/api/v1", tags=["logs"])

_DEFAULT_WINDOW = timedelta(hours=24)
_STATUS_VALUES = {"success", "error", "blocked"}

# Requests rejected for lack of budget carry this status code (see the
# gateway's ratelimit/budget middleware) — broken out from generic 4xx/5xx so
# "blocked" reads as a spend-control event, not an application error.
_BUDGET_BLOCKED_STATUS = 402


def _encode_cursor(ts: datetime, request_id: Any) -> str:
    raw = f"{ts.isoformat()}|{request_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        ts_str, id_str = raw.split("|", 1)
        return datetime.fromisoformat(ts_str), UUID(id_str)
    except Exception:
        raise HTTPException(status_code=400, detail="Malformed cursor") from None


def _vs_acpi_pct(cost_usd: float, acpi_bench_usd: float) -> float | None:
    if not acpi_bench_usd:
        return None
    return round(((cost_usd - acpi_bench_usd) / acpi_bench_usd) * 100, 2)


def _build_filters(
    workspace_id: UUID,
    range_from: datetime,
    range_to: datetime,
    provider: str | None,
    status: str | None,
    feature_tag: str | None,
    session_id: str | None,
    model: str | None,
) -> tuple[list[str], list[Any]]:
    if status is not None and status not in _STATUS_VALUES:
        raise HTTPException(
            status_code=400, detail=f"status must be one of {sorted(_STATUS_VALUES)}"
        )

    conditions = ['workspace_id = $1', '"timestamp" >= $2', '"timestamp" < $3']
    params: list[Any] = [workspace_id, range_from, range_to]

    def eq(column: str, value: Any) -> None:
        params.append(value)
        conditions.append(f"{column} = ${len(params)}")

    if provider:
        eq("provider", provider)
    if feature_tag:
        eq("feature_tag", feature_tag)
    if session_id:
        eq("session_id", session_id)
    if model:
        eq("model_id", model)

    if status == "success":
        conditions.append("status_code < 400")
    elif status == "error":
        conditions.append(f"status_code >= 400 AND status_code != {_BUDGET_BLOCKED_STATUS}")
    elif status == "blocked":
        conditions.append(f"status_code = {_BUDGET_BLOCKED_STATUS}")

    return conditions, params


def _resolve_range(from_: datetime | None, to: datetime | None) -> tuple[datetime, datetime]:
    range_to = to or datetime.now(timezone.utc)
    range_from = from_ or (range_to - _DEFAULT_WINDOW)
    return range_from, range_to


@router.get("/logs")
async def list_logs(
    from_: datetime | None = Query(None, alias="from"),
    to: datetime | None = Query(None),
    provider: str | None = Query(None),
    status: str | None = Query(None),
    feature_tag: str | None = Query(None),
    session_id: str | None = Query(None),
    model: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = Query(None),
    workspace_id: UUID = Depends(current_workspace),
) -> dict:
    range_from, range_to = _resolve_range(from_, to)
    conditions, params = _build_filters(
        workspace_id, range_from, range_to, provider, status, feature_tag, session_id, model
    )

    count_row = await db.fetchrow(
        f'SELECT COUNT(*) AS total FROM usage_records WHERE {" AND ".join(conditions)}',
        *params,
    )
    total = int(count_row["total"]) if count_row else 0

    page_conditions = list(conditions)
    page_params = list(params)
    if cursor:
        cursor_ts, cursor_id = _decode_cursor(cursor)
        page_params.append(cursor_ts)
        page_params.append(cursor_id)
        page_conditions.append(
            f'("timestamp", request_id) < (${len(page_params) - 1}, ${len(page_params)})'
        )

    page_params.append(limit + 1)  # one extra row tells us whether there's a next page
    rows = await db.fetch(
        f"""
        SELECT request_id, "timestamp", provider, model_id,
               input_tokens, output_tokens, cost_usd, acpi_bench_usd, overpay_usd,
               latency_ms, status_code, feature_tag, session_id, session_name,
               step_name, priced
        FROM usage_records
        WHERE {" AND ".join(page_conditions)}
        ORDER BY "timestamp" DESC, request_id DESC
        LIMIT ${len(page_params)}
        """,
        *page_params,
    )

    has_more = len(rows) > limit
    page = rows[:limit]
    next_cursor = _encode_cursor(page[-1]["timestamp"], page[-1]["request_id"]) if has_more and page else None

    return {
        "rows": [
            {
                "request_id": str(row["request_id"]),
                "timestamp": row["timestamp"].isoformat(),
                "provider": row["provider"],
                "model_id": row["model_id"],
                "input_tokens": int(row["input_tokens"] or 0),
                "output_tokens": int(row["output_tokens"] or 0),
                "cost_usd": round(f(row["cost_usd"]), 6),
                "acpi_bench_usd": round(f(row["acpi_bench_usd"]), 6),
                "overpay_usd": round(f(row["overpay_usd"]), 6),
                "vs_acpi_pct": _vs_acpi_pct(f(row["cost_usd"]), f(row["acpi_bench_usd"])),
                "latency_ms": int(row["latency_ms"] or 0),
                "status_code": row["status_code"],
                "feature_tag": row["feature_tag"],
                "session_id": row["session_id"],
                "session_name": row["session_name"],
                "step_name": row["step_name"],
                "priced": bool(row["priced"]),
            }
            for row in page
        ],
        "total": total,
        "next_cursor": next_cursor,
    }


@router.get("/logs/summary")
async def logs_summary(
    from_: datetime | None = Query(None, alias="from"),
    to: datetime | None = Query(None),
    provider: str | None = Query(None),
    status: str | None = Query(None),
    feature_tag: str | None = Query(None),
    session_id: str | None = Query(None),
    model: str | None = Query(None),
    workspace_id: UUID = Depends(current_workspace),
) -> dict:
    range_from, range_to = _resolve_range(from_, to)
    conditions, params = _build_filters(
        workspace_id, range_from, range_to, provider, status, feature_tag, session_id, model
    )

    row = await db.fetchrow(
        f"""
        SELECT
            COUNT(*)                                                          AS total_requests,
            COALESCE(SUM(cost_usd), 0)                                        AS total_cost_usd,
            COALESCE(AVG(latency_ms), 0)                                      AS avg_latency_ms,
            COALESCE(AVG((status_code >= 400)::int) * 100, 0)                 AS error_rate_pct
        FROM usage_records
        WHERE {" AND ".join(conditions)}
        """,
        *params,
    )

    return {
        "total_requests": int(row["total_requests"] or 0) if row else 0,
        "total_cost_usd": round(f(row["total_cost_usd"]), 6) if row else 0.0,
        "avg_latency_ms": round(f(row["avg_latency_ms"]), 0) if row else 0.0,
        "error_rate_pct": round(f(row["error_rate_pct"]), 2) if row else 0.0,
    }
