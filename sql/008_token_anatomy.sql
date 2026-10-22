-- Token anatomy and cache-aware cost parts on the ledger.
--
-- All nullable: rows written before this migration, error rows, and requests
-- whose provider reports no usage simply have no anatomy. cache reads keep
-- living in the existing cached_input_tokens column.
--
-- Applied automatically by the gateway at startup (services/db.py), because the
-- gateway is the writer: if these columns are missing its COPY fails and ledger
-- rows are lost. Keep this file and that list in sync.
ALTER TABLE usage_records ADD COLUMN IF NOT EXISTS system_tokens           INTEGER;
ALTER TABLE usage_records ADD COLUMN IF NOT EXISTS tool_tokens             INTEGER;
ALTER TABLE usage_records ADD COLUMN IF NOT EXISTS history_tokens          INTEGER;
ALTER TABLE usage_records ADD COLUMN IF NOT EXISTS user_tokens             INTEGER;
ALTER TABLE usage_records ADD COLUMN IF NOT EXISTS tool_count              INTEGER;
ALTER TABLE usage_records ADD COLUMN IF NOT EXISTS cache_write_tokens      BIGINT;
ALTER TABLE usage_records ADD COLUMN IF NOT EXISTS uncached_input_tokens   BIGINT;
ALTER TABLE usage_records ADD COLUMN IF NOT EXISTS max_output_tokens       INTEGER;
-- Salted SHA-256 (16 hex) of system + tool definitions. Not reversible to content.
ALTER TABLE usage_records ADD COLUMN IF NOT EXISTS prefix_hash             TEXT;
ALTER TABLE usage_records ADD COLUMN IF NOT EXISTS uncached_input_cost_usd NUMERIC(16, 8);
ALTER TABLE usage_records ADD COLUMN IF NOT EXISTS cache_write_cost_usd    NUMERIC(16, 8);
ALTER TABLE usage_records ADD COLUMN IF NOT EXISTS cache_read_cost_usd     NUMERIC(16, 8);
ALTER TABLE usage_records ADD COLUMN IF NOT EXISTS output_cost_usd         NUMERIC(16, 8);
ALTER TABLE usage_records ADD COLUMN IF NOT EXISTS prefix_cache_saving_usd NUMERIC(16, 8);
