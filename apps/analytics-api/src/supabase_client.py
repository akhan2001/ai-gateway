"""Async Supabase client for the agent_sessions table.

Separate connection from the TimescaleDB pool in db.py — see
sql/supabase/001_agent_sessions.sql for why agent_sessions lives on Supabase
instead. Uses the service_role key, which bypasses Supabase RLS entirely
(agent_sessions has RLS enabled with zero policies — see that migration for
why); every caller here is trusted to scope its own queries by workspace_id.
"""

from __future__ import annotations

import logging
import os

from supabase import AsyncClient, create_async_client

log = logging.getLogger(__name__)


def _url() -> str:
    explicit = os.getenv("SUPABASE_URL")
    if explicit:
        return explicit
    project_id = os.getenv("PROJECT_ID")
    if not project_id:
        raise RuntimeError("Set SUPABASE_URL (or PROJECT_ID) to reach Supabase.")
    return f"https://{project_id}.supabase.co"


def _key() -> str:
    key = os.getenv("SUPABASE_SERVICE_KEY")
    if not key:
        raise RuntimeError("SUPABASE_SERVICE_KEY is not set.")
    return key


class SupabaseConn:
    def __init__(self) -> None:
        self.client: AsyncClient | None = None

    async def start(self) -> None:
        """Best-effort — the rest of the API (budgets, summary, usage) must stay
        up even if Supabase isn't configured yet. session_sync_worker checks
        `client is not None` before every cycle and no-ops otherwise."""
        try:
            self.client = await create_async_client(_url(), _key())
            log.info("supabase client up")
        except Exception:
            log.warning(
                "supabase client not started (SUPABASE_URL/SUPABASE_SERVICE_KEY "
                "missing or unreachable) — session sync will be a no-op",
                exc_info=True,
            )

    async def stop(self) -> None:
        self.client = None


supabase = SupabaseConn()
