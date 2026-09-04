-- Monthly spend budgets and the alert dedup ledger.
--
-- Applied automatically on analytics-api startup — see
-- apps/analytics-api/src/migrations.py, which embeds these same statements
-- (the Docker build context doesn't include this file, so it can't read this
-- one at runtime). This file is the reviewable source of truth and is what
-- local dev applies via docker-compose's initdb mount; keep both in sync.
--
-- One budget per workspace (UNIQUE on workspace_id): the MVP is "warn me
-- before I overspend this month", not multiple concurrent budgets.
--
-- budget_alerts_sent exists purely to make alerting idempotent. The checker
-- runs hourly and re-evaluates every workspace's thresholds each time, so
-- without this table a workspace sitting above 80% would get a fresh email
-- every single hour instead of once per threshold per month.

CREATE TABLE IF NOT EXISTS budgets (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id       UUID NOT NULL REFERENCES workspaces (id) ON DELETE CASCADE UNIQUE,
    monthly_limit_usd  NUMERIC(12, 2) NOT NULL CHECK (monthly_limit_usd > 0),
    alert_pct          INTEGER NOT NULL DEFAULT 80 CHECK (alert_pct > 0 AND alert_pct <= 100),
    alert_email        TEXT NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS budget_alerts_sent (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id  UUID NOT NULL REFERENCES workspaces (id) ON DELETE CASCADE,
    alert_type    TEXT NOT NULL,  -- 'warning' (alert_pct) | 'exceeded' (100%) | 'critical' (120%)
    month         TEXT NOT NULL,  -- 'YYYY-MM', so a calendar-month rollover naturally resets alerts
    sent_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (workspace_id, alert_type, month)
);
