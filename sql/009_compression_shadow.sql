-- Compression shadow mode: what safe compression WOULD have saved per request.
-- Nothing is compressed on real traffic. Counts and dollars only; no content.
-- Applied by the gateway at startup (services/db.py), check-first.

ALTER TABLE usage_records ADD COLUMN IF NOT EXISTS shadow_compressed_tokens BIGINT;
ALTER TABLE usage_records ADD COLUMN IF NOT EXISTS shadow_saving_tokens     BIGINT;
-- Net of caching: saved tokens priced at the rate their position was billed at.
ALTER TABLE usage_records ADD COLUMN IF NOT EXISTS shadow_saving_usd        NUMERIC(16, 8);
-- A saving fell inside the provider-cached prefix.
ALTER TABLE usage_records ADD COLUMN IF NOT EXISTS shadow_prefix_changed    BOOLEAN;
-- Share of requests sampled when this row was; analytics divides by it.
ALTER TABLE usage_records ADD COLUMN IF NOT EXISTS shadow_sample_rate       REAL;

-- Per-workspace opt-in, set from the dashboard's Optimize page.
ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS compression_shadow BOOLEAN NOT NULL DEFAULT FALSE;
