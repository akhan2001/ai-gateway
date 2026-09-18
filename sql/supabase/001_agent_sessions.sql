-- Agent session rollups — targets the Supabase Postgres instance, NOT
-- TimescaleDB. Everything else under sql/ runs against the Railway
-- TimescaleDB (via analytics-api's own DATABASE_URL); this one is a separate
-- connection and is not auto-applied by migrations.py. Apply it by hand
-- (Supabase SQL editor, or `psql "$SUPABASE_DB_URL" -f this file`) until a
-- session worker exists to run it programmatically.
--
-- Why Supabase and not TimescaleDB: one row per agent run (not per request —
-- low write frequency), viewed and annotated by dashboard users (outcome
-- recording), and wants access control. usage_records stays the
-- high-frequency, time-series ledger on TimescaleDB; workspace_id below is a
-- soft reference to its `workspaces.id` — no FK constraint is possible across
-- databases.
--
-- Why no org/team RLS: Tokenix's tenancy is one workspace per Clerk user
-- (workspaces.clerk_user_id, ai-gateway/sql/002_clerk_identity.sql) — there is
-- no org, team, or memberships table to key policies off. RLS is enabled with
-- zero policies below, which denies every role except service_role by
-- default: the analytics API connects with the Supabase service role key and
-- enforces workspace scoping itself, the same way auth checks live in
-- application code elsewhere in this project rather than in the data/routing
-- layer. Add real per-user policies only once Clerk sessions are federated
-- into Supabase Auth (so auth.jwt() carries a usable Clerk user id) — not
-- before, since auth.uid() has nothing to key off today.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS agent_sessions (
    session_id        TEXT PRIMARY KEY,
    workspace_id      UUID NOT NULL,

    name              TEXT,
    status            TEXT NOT NULL DEFAULT 'running'
                           CHECK (status IN ('running', 'completed', 'failed', 'abandoned')),

    started_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at      TIMESTAMPTZ,
    last_activity_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    total_cost_usd    NUMERIC(16, 8) NOT NULL DEFAULT 0,
    total_tokens      BIGINT NOT NULL DEFAULT 0,
    total_requests    INTEGER NOT NULL DEFAULT 0,
    models_used       TEXT[] NOT NULL DEFAULT '{}',

    feature_tag       TEXT,
    outcome           TEXT,
    outcome_value     NUMERIC(16, 4),
    cost_per_outcome  NUMERIC(16, 8),

    loop_detected     BOOLEAN NOT NULL DEFAULT FALSE,
    loop_step         TEXT,
    loop_count        INTEGER,

    metadata          JSONB NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS agent_sessions_workspace_started_idx
    ON agent_sessions (workspace_id, started_at DESC);
CREATE INDEX IF NOT EXISTS agent_sessions_workspace_running_idx
    ON agent_sessions (workspace_id, status)
    WHERE status = 'running';

ALTER TABLE agent_sessions ENABLE ROW LEVEL SECURITY;
-- Intentionally no policies — see note above.
