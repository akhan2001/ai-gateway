"""Startup migration runner for sql/003_budgets.sql.

The analytics-api Docker image only bundles `src/` (build context is
`apps/analytics-api` — see its Dockerfile — which does not include the
repo-root `sql/` directory), so the statements are embedded here rather than
read from disk at runtime. `sql/003_budgets.sql` stays the reviewable source
of truth (and is what local dev applies via docker-compose's initdb mount);
keep the two in sync if this migration ever changes.

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
        log.info("startup migration 003_budgets applied (or already present)")
    except Exception:
        log.exception("startup migration 003_budgets failed")
