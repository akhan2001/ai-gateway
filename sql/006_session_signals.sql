-- Two more per-request signals for agent sessions, alongside the session_id /
-- step_name / step_sequence columns added in 005_agent_sessions.sql.
--
-- session_name carries the human-readable label from Tokenix-Session-Name —
-- there's nowhere else for it to reach the session worker (Phase 3), since
-- the worker only ever sees usage_records, not the original request headers.
--
-- session_status carries Tokenix-Session-Status when it's "completed" or
-- "failed" (NULL otherwise). It's a one-shot signal, not the session's
-- current status — the worker reads it off the row that carried it and uses
-- it to close the session immediately instead of waiting for the inactivity
-- timeout.
--
-- Applied automatically on analytics-api startup — see
-- apps/analytics-api/src/migrations.py, same pattern as 005.

ALTER TABLE usage_records ADD COLUMN IF NOT EXISTS session_name TEXT;
ALTER TABLE usage_records ADD COLUMN IF NOT EXISTS session_status TEXT;
