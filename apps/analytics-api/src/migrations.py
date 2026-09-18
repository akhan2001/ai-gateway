"""Startup migration runner for sql/003_budgets.sql, sql/005_agent_sessions.sql,
sql/006_session_signals.sql, and sql/007_auto_session.sql.

The analytics-api Docker image only bundles `src/` (build context is
`apps/analytics-api` — see its Dockerfile — which does not include the
repo-root `sql/` directory), so the statements are embedded here rather than
read from disk at runtime. The numbered files under `sql/` stay the reviewable
source of truth (and are what local dev applies via docker-compose's initdb
mount); keep them in sync if a migration here ever changes.

sql/supabase/001_agent_sessions.sql is NOT embedded here — it targets a
separate Supabase connection, not the TimescaleDB pool this runner uses. Apply
it by hand until a Supabase client is wired into this service.

Every statement is `IF NOT EXISTS`, so running this on every startup — every
deploy, every replica — is safe and idempotent.
"""

from __future__ import annotations

import logging

from .db import db

log = logging.getLogger(__name__)

_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS budgets (
        id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        workspace_id       UUID NOT NULL REFERENCES workspaces (id) ON DELETE CASCADE UNIQUE,
        monthly_limit_usd  NUMERIC(12, 2) NOT NULL CHECK (monthly_limit_usd > 0),
        alert_pct          INTEGER NOT NULL DEFAULT 80 CHECK (alert_pct > 0 AND alert_pct <= 100),
        alert_email        TEXT NOT NULL,
        created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS budget_alerts_sent (
        id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        workspace_id  UUID NOT NULL REFERENCES workspaces (id) ON DELETE CASCADE,
        alert_type    TEXT NOT NULL,
        month         TEXT NOT NULL,
        sent_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE (workspace_id, alert_type, month)
    )
    """,
    """
    ALTER TABLE usage_records ADD COLUMN IF NOT EXISTS session_id TEXT
    """,
    """
    ALTER TABLE usage_records ADD COLUMN IF NOT EXISTS step_name TEXT
    """,
    """
    ALTER TABLE usage_records ADD COLUMN IF NOT EXISTS step_sequence INTEGER
    """,
    """
    CREATE INDEX IF NOT EXISTS usage_records_session_idx
        ON usage_records (session_id, "timestamp" DESC)
        WHERE session_id IS NOT NULL
    """,
    """
    ALTER TABLE usage_records ADD COLUMN IF NOT EXISTS session_name TEXT
    """,
    """
    ALTER TABLE usage_records ADD COLUMN IF NOT EXISTS session_status TEXT
    """,
    """
    ALTER TABLE usage_records ADD COLUMN IF NOT EXISTS auto_generated BOOLEAN NOT NULL DEFAULT FALSE
    """,
]


async def run_startup_migrations() -> None:
    """Apply pending embedded migrations. Never raises — a migration failure
    is logged, not fatal, since it must not take the whole API down."""
    assert db.pool is not None, "database pool not started"
    try:
        async with db.pool.acquire() as conn:
            async with conn.transaction():
                for statement in _STATEMENTS:
                    await conn.execute(statement)
        log.info(
            "startup migrations 003_budgets, 005_agent_sessions, 006_session_signals, "
            "007_auto_session applied (or already present)"
        )
    except Exception:
        log.exception("startup migrations failed")
