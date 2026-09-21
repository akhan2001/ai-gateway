"""GET /api/v1/sessions, GET /api/v1/sessions/{id}, POST /api/v1/sessions/{id}/outcome.

Sessions live on Supabase (apps/analytics-api/src/jobs/session_sync.py builds
agent_sessions rows out of tagged usage_records) -- these routes read that,
then join in per-step / cost detail from TimescaleDB, the same shape as the
org spend rollup described when the Supabase split was designed: two reads,
combined in the API layer, never a cross-database FK.

Workspace scoping is enforced here, in application code, not by Supabase RLS
-- agent_sessions has RLS enabled with zero policies and this service holds
the service_role key, so every query below filters by workspace_id by hand.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..auth import current_workspace
from ..db import db, f
from ..supabase_client import supabase

router = APIRouter(prefix="/api/v1", tags=["sessions"])

# A session_name/step_name repeated more than this many times in one session
# is treated as a stuck loop, not a long-running workflow.
_LOOP_THRESHOLD = 5


def _require_supabase():
    if supabase.client is None:
        raise HTTPException(status_code=503, detail="Session tracking is not configured")
    return supabase.client


def _parse_ts(value: Any) -> datetime | None:
    """Supabase (via PostgREST/JSON) returns timestamps as ISO strings, not
    datetimes -- unlike asyncpg, which hands back real datetime objects."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _duration_seconds(started_at: Any, completed_at: Any) -> float:
    started = _parse_ts(started_at)
    if started is None:
        return 0.0
    end = _parse_ts(completed_at) or datetime.now(timezone.utc)
    return max((end - started).total_seconds(), 0.0)


def _vs_acpi_pct(cost_usd: float, acpi_bench_usd: float) -> float | None:
    if not acpi_bench_usd:
        return None
    return round(((cost_usd - acpi_bench_usd) / acpi_bench_usd) * 100, 2)


def _session_summary(row: dict[str, Any], acpi_bench_usd: float) -> dict[str, Any]:
    total_cost = f(row.get("total_cost_usd"))
    outcome_value = row.get("outcome_value")
    cost_per_outcome = row.get("cost_per_outcome")
    return {
        "session_id": row["session_id"],
        "name": row.get("name"),
        "status": row["status"],
        "feature_tag": row.get("feature_tag"),
        "started_at": row.get("started_at"),
        "completed_at": row.get("completed_at"),
        "duration_seconds": round(_duration_seconds(row.get("started_at"), row.get("completed_at")), 1),
        "total_cost_usd": round(total_cost, 6),
        "acpi_bench_usd": round(acpi_bench_usd, 6),
        "vs_acpi_pct": _vs_acpi_pct(total_cost, acpi_bench_usd),
        "total_requests": int(row.get("total_requests") or 0),
        "total_tokens": int(row.get("total_tokens") or 0),
        "models_used": row.get("models_used") or [],
        "outcome": row.get("outcome"),
        "outcome_value": f(outcome_value) if outcome_value is not None else None,
        "cost_per_outcome": f(cost_per_outcome) if cost_per_outcome is not None else None,
    }


async def _acpi_bench_by_session(session_ids: list[str], workspace_id: UUID) -> dict[str, float]:
    if not session_ids:
        return {}
    rows = await db.fetch(
        """
        SELECT session_id, COALESCE(SUM(acpi_bench_usd), 0) AS acpi_bench_usd
        FROM usage_records
        WHERE session_id = ANY($1::text[]) AND workspace_id = $2
        GROUP BY session_id
        """,
        session_ids,
        workspace_id,
    )
    return {row["session_id"]: f(row["acpi_bench_usd"]) for row in rows}


@router.get("/sessions")
async def list_sessions(
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
    feature_tag: str | None = Query(None),
    status: str | None = Query(None),
    workspace_id: UUID = Depends(current_workspace),
) -> dict:
    client = _require_supabase()

    query = (
        client.table("agent_sessions")
        .select("*", count="exact")
        .eq("workspace_id", str(workspace_id))
        .order("started_at", desc=True)
        .range(offset, offset + limit - 1)
    )
    if feature_tag:
        query = query.eq("feature_tag", feature_tag)
    if status:
        query = query.eq("status", status)

    result = await query.execute()
    rows = result.data or []

    bench_by_id = await _acpi_bench_by_session([r["session_id"] for r in rows], workspace_id)
    sessions = [_session_summary(row, bench_by_id.get(row["session_id"], 0.0)) for row in rows]

    return {
        "sessions": sessions,
        "total": result.count if result.count is not None else len(sessions),
        "limit": limit,
        "offset": offset,
    }


@router.get("/sessions/{session_id}")
async def session_detail(
    session_id: str, workspace_id: UUID = Depends(current_workspace)
) -> dict:
    client = _require_supabase()

    result = (
        await client.table("agent_sessions")
        .select("*")
        .eq("session_id", session_id)
        .eq("workspace_id", str(workspace_id))
        .execute()
    )
    rows = result.data or []
    if not rows:
        raise HTTPException(status_code=404, detail="Session not found")
    row = rows[0]

    steps = await db.fetch(
        """
        SELECT step_sequence, step_name, model_id, provider,
               input_tokens, output_tokens, cost_usd, acpi_bench_usd,
               latency_ms, status_code, "timestamp", priced
        FROM usage_records
        WHERE session_id = $1 AND workspace_id = $2
        ORDER BY "timestamp" ASC
        """,
        session_id,
        workspace_id,
    )

    step_list: list[dict[str, Any]] = []
    step_counts: dict[str, int] = {}
    acpi_bench_usd = 0.0

    for i, step in enumerate(steps, start=1):
        name = step["step_name"] or f"step {i}"
        step_counts[name] = step_counts.get(name, 0) + 1
        cost = f(step["cost_usd"])
        bench = f(step["acpi_bench_usd"])
        acpi_bench_usd += bench
        step_list.append(
            {
                "sequence": step["step_sequence"] if step["step_sequence"] is not None else i,
                "step_name": step["step_name"],
                "model_id": step["model_id"],
                "provider": step["provider"],
                "input_tokens": int(step["input_tokens"] or 0),
                "output_tokens": int(step["output_tokens"] or 0),
                "cost_usd": round(cost, 6),
                "acpi_bench_usd": round(bench, 6),
                "vs_acpi_pct": _vs_acpi_pct(cost, bench),
                "latency_ms": int(step["latency_ms"] or 0),
                "status_code": step["status_code"],
                "timestamp": step["timestamp"].isoformat(),
                "priced": bool(step["priced"]),
            }
        )

    loop_step = max(step_counts, key=lambda k: step_counts[k]) if step_counts else None
    loop_count = step_counts.get(loop_step, 0) if loop_step else 0
    loop_detected = loop_count > _LOOP_THRESHOLD

    summary = _session_summary(row, acpi_bench_usd)
    summary["steps"] = step_list
    summary["loop_detected"] = loop_detected
    summary["loop_step"] = loop_step if loop_detected else None
    summary["loop_count"] = loop_count if loop_detected else None
    return summary


class RecordOutcome(BaseModel):
    outcome: str = Field(min_length=1, max_length=200)
    value: float = Field(gt=0)


@router.post("/sessions/{session_id}/outcome")
async def record_outcome(
    session_id: str,
    body: RecordOutcome,
    workspace_id: UUID = Depends(current_workspace),
) -> dict:
    client = _require_supabase()

    existing = (
        await client.table("agent_sessions")
        .select("session_id, total_cost_usd")
        .eq("session_id", session_id)
        .eq("workspace_id", str(workspace_id))
        .execute()
    )
    rows = existing.data or []
    if not rows:
        raise HTTPException(status_code=404, detail="Session not found")

    total_cost = f(rows[0].get("total_cost_usd"))
    cost_per_outcome = round(total_cost / body.value, 6)

    await (
        client.table("agent_sessions")
        .update(
            {
                "outcome": body.outcome,
                "outcome_value": body.value,
                "cost_per_outcome": cost_per_outcome,
            }
        )
        .eq("session_id", session_id)
        .execute()
    )

    return {
        "session_id": session_id,
        "outcome": body.outcome,
        "outcome_value": body.value,
        "cost_per_outcome": cost_per_outcome,
        "total_cost_usd": round(total_cost, 6),
    }
