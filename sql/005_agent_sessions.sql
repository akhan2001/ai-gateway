-- Session/step tagging on the usage ledger.
--
-- The session rollup itself (agent_sessions) does NOT live here — it lives on
-- Supabase (see sql/supabase/001_agent_sessions.sql for why: low write
-- frequency, dashboard-user-facing, wants RLS — a different profile than this
-- high-frequency hypertable). This file only adds the columns the session
-- worker groups usage_records by. session_id is a soft reference to
-- Supabase's agent_sessions.session_id — no FK constraint is possible across
-- databases.
--
-- Applied automatically on analytics-api startup — see
-- apps/analytics-api/src/migrations.py, same pattern as 003_budgets.sql. This
-- file is the reviewable source of truth; keep both in sync.

ALTER TABLE usage_records ADD COLUMN IF NOT EXISTS session_id TEXT;
ALTER TABLE usage_records ADD COLUMN IF NOT EXISTS step_name TEXT;
ALTER TABLE usage_records ADD COLUMN IF NOT EXISTS step_sequence INTEGER;

CREATE INDEX IF NOT EXISTS usage_records_session_idx
    ON usage_records (session_id, "timestamp" DESC)
    WHERE session_id IS NOT NULL;
