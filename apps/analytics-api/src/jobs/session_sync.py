"""Turns tagged usage_records rows (TimescaleDB) into agent_sessions rows
(Supabase) every 60 seconds.

Mirrors apps/analytics-api/src/budget_checker.py: a start/stop pair around an
asyncio.create_task loop that sleeps first, then runs, and never lets one bad
iteration kill the background task.

Five stages per cycle, run in this order because later stages depend on
earlier ones having settled the 'running' set:
  1. create   — new session_ids seen in the last 24h get an agent_sessions row
  2. totals   — every 'running' session gets its cost/tokens/requests resummed
  3. timeout  — 'running' sessions idle >10min are marked 'completed'
  4. abandon  — 'running' sessions older than 24h are marked 'abandoned'
  5. explicit — Tokenix-Session-Status: completed/failed closes immediately,
                without waiting for stage 3's timeout
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from ..db import db
from ..supabase_client import supabase

log = logging.getLogger(__name__)

_SYNC_INTERVAL_SECONDS = 60
_NEW_SESSION_LOOKBACK = timedelta(hours=24)
_INACTIVITY_TIMEOUT = timedelta(minutes=10)
_ABANDONED_AFTER = timedelta(hours=24)
_EXPLICIT_COMPLETION_LOOKBACK = timedelta(minutes=5)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _create_new_sessions() -> None:
    candidates = await db.fetch(
        """
        SELECT
            session_id,
            workspace_id,
            MIN("timestamp") AS started_at,
            MAX(session_name) FILTER (WHERE session_name IS NOT NULL) AS name,
            MAX(feature_tag) FILTER (WHERE feature_tag IS NOT NULL) AS feature_tag
        FROM usage_records
        WHERE session_id IS NOT NULL
          AND "timestamp" > NOW() - $1::interval
        GROUP BY session_id, workspace_id
        """,
        _NEW_SESSION_LOOKBACK,
    )
    if not candidates:
        return

    ids = [c["session_id"] for c in candidates]
    existing = await supabase.client.table("agent_sessions").select("session_id").in_(
        "session_id", ids
    ).execute()
    known = {row["session_id"] for row in existing.data}

    new_rows: list[dict[str, Any]] = [
        {
            "session_id": c["session_id"],
            "workspace_id": str(c["workspace_id"]),
            "name": c["name"],
            "feature_tag": c["feature_tag"],
            "started_at": c["started_at"].isoformat(),
            "status": "running",
            "last_activity_at": _now_iso(),
        }
        for c in candidates
        if c["session_id"] not in known
    ]
    if not new_rows:
        return

    # ignore_duplicates guards against a race with another replica of this
    # worker inserting the same session_id between the check above and here.
    await supabase.client.table("agent_sessions").upsert(
        new_rows, on_conflict="session_id", ignore_duplicates=True
    ).execute()
    log.info("session sync: created %d new session(s)", len(new_rows))


async def _update_running_totals() -> None:
    running = await supabase.client.table("agent_sessions").select("session_id").eq(
        "status", "running"
    ).execute()
    session_ids = [row["session_id"] for row in running.data]
    if not session_ids:
        return

    totals = await db.fetch(
        """
        SELECT
            session_id,
            COALESCE(SUM(cost_usd), 0) AS total_cost_usd,
            COALESCE(SUM(input_tokens + output_tokens), 0) AS total_tokens,
            COUNT(*) AS total_requests,
            ARRAY_AGG(DISTINCT model_id) AS models_used,
            MAX("timestamp") AS last_activity_at
        FROM usage_records
        WHERE session_id = ANY($1::text[])
        GROUP BY session_id
        """,
        session_ids,
    )
    for row in totals:
        await supabase.client.table("agent_sessions").upsert(
            {
                "session_id": row["session_id"],
                "total_cost_usd": float(row["total_cost_usd"]),
                "total_tokens": int(row["total_tokens"]),
                "total_requests": int(row["total_requests"]),
                "models_used": row["models_used"],
                "last_activity_at": row["last_activity_at"].isoformat(),
            },
            on_conflict="session_id",
        ).execute()


async def _apply_inactivity_timeout() -> None:
    cutoff = (datetime.now(timezone.utc) - _INACTIVITY_TIMEOUT).isoformat()
    await supabase.client.table("agent_sessions").update(
        {"status": "completed", "completed_at": _now_iso()}
    ).eq("status", "running").lt("last_activity_at", cutoff).execute()


async def _apply_abandoned_cleanup() -> None:
    cutoff = (datetime.now(timezone.utc) - _ABANDONED_AFTER).isoformat()
    await supabase.client.table("agent_sessions").update(
        {"status": "abandoned", "completed_at": _now_iso()}
    ).eq("status", "running").lt("started_at", cutoff).execute()


async def _apply_explicit_completions() -> None:
    signals = await db.fetch(
        """
        SELECT DISTINCT ON (session_id) session_id, session_status
        FROM usage_records
        WHERE session_status IN ('completed', 'failed')
          AND session_id IS NOT NULL
          AND "timestamp" > NOW() - $1::interval
        ORDER BY session_id, "timestamp" DESC
        """,
        _EXPLICIT_COMPLETION_LOOKBACK,
    )
    for row in signals:
        await supabase.client.table("agent_sessions").update(
            {"status": row["session_status"], "completed_at": _now_iso()}
        ).eq("session_id", row["session_id"]).execute()


_STAGES = (
    _create_new_sessions,
    _update_running_totals,
    _apply_inactivity_timeout,
    _apply_abandoned_cleanup,
    _apply_explicit_completions,
)


async def sync_once() -> None:
    if supabase.client is None:
        log.debug("session sync skipped: supabase client not configured")
        return
    # Each stage runs in isolation: earlier stages (e.g. totals) can depend on
    # bad or unexpected row data and raise, but a broken stage must not starve
    # every stage after it forever — explicit completions in particular must
    # keep running each cycle even if totals is stuck erroring on some row.
    for stage in _STAGES:
        try:
            await stage()
        except Exception:
            log.exception("session sync stage %s failed", stage.__name__)


class SessionSyncWorker:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None

    async def _run_forever(self) -> None:
        while True:
            await asyncio.sleep(_SYNC_INTERVAL_SECONDS)
            try:
                await sync_once()
            except Exception:  # never let the sync loop die
                log.exception("session sync iteration failed")

    def start(self) -> None:
        self._task = asyncio.create_task(self._run_forever())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None


session_sync_worker = SessionSyncWorker()
