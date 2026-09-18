-- Marks rows the gateway grouped into a session automatically (no
-- Tokenix-Session header sent) vs. one a customer tagged explicitly. Session
-- rollups treat both the same way; this is just provenance for the dashboard
-- to label as "Auto session" and, later, to let customers filter it out.
--
-- Applied automatically on analytics-api startup — see
-- apps/analytics-api/src/migrations.py, same pattern as 005/006.

ALTER TABLE usage_records ADD COLUMN IF NOT EXISTS auto_generated BOOLEAN NOT NULL DEFAULT FALSE;
