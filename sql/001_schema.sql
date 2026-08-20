-- Tokenix MVP schema (TimescaleDB / Postgres 15+)
--
-- The ledger is append-only: a re-priced request is a new row, never an
-- in-place update, so spend history stays auditable the same way the ACPI
-- snapshots do.

CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- gen_random_uuid()

-- ---------------------------------------------------------------------------
-- Tenancy
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS workspaces (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT        NOT NULL,
    enabled     BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Customer-facing `txk-` keys.
--
-- `key_hash` is SHA-256, not bcrypt. Bcrypt cannot be used here: verifying it
-- requires trying every stored hash one by one (the salt lives inside each
-- hash), which is a full table scan per request on the hot path. SHA-256 gives
-- a single indexed lookup. That is safe *because* the key is 32 bytes of
-- CSPRNG output, not a user-chosen password — there is no dictionary to attack
-- and nothing for a slow KDF to buy us.
CREATE TABLE IF NOT EXISTS api_keys (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id  UUID        NOT NULL REFERENCES workspaces (id) ON DELETE CASCADE,
    key_hash      TEXT        NOT NULL UNIQUE,
    key_prefix    TEXT        NOT NULL,   -- first 12 chars, for display only
    revoked       BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at  TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS api_keys_workspace_idx
    ON api_keys (workspace_id) WHERE NOT revoked;

-- Provider credentials, Fernet-encrypted at rest.
CREATE TABLE IF NOT EXISTS provider_keys (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id   UUID        NOT NULL REFERENCES workspaces (id) ON DELETE CASCADE,
    provider       TEXT        NOT NULL,
    encrypted_key  TEXT        NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (workspace_id, provider)
);

-- ---------------------------------------------------------------------------
-- The ledger
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS usage_records (
    request_id           UUID        NOT NULL,
    "timestamp"          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    workspace_id         UUID        NOT NULL,

    provider             TEXT        NOT NULL,
    model_id             TEXT        NOT NULL,
    feature_tag          TEXT,
    workload_tag         TEXT,

    input_tokens         BIGINT      NOT NULL DEFAULT 0,
    output_tokens        BIGINT      NOT NULL DEFAULT 0,
    cached_input_tokens  BIGINT      NOT NULL DEFAULT 0,

    -- What the request actually cost at the model's list price.
    cost_usd             NUMERIC(16, 8) NOT NULL DEFAULT 0,
    -- What the same token volume costs at the market-wide ACPI rate.
    acpi_bench_usd       NUMERIC(16, 8) NOT NULL DEFAULT 0,
    -- cost_usd - acpi_bench_usd. Positive = paying above market.
    overpay_usd          NUMERIC(16, 8) NOT NULL DEFAULT 0,
    -- The model's intelligence-per-dollar score when priced, kept so old rows
    -- stay interpretable after the index moves.
    acpi_score           DOUBLE PRECISION,
    acpi_dataset_version TIMESTAMPTZ,

    latency_ms           INTEGER     NOT NULL DEFAULT 0,
    status_code          SMALLINT    NOT NULL DEFAULT 0,
    is_stream            BOOLEAN     NOT NULL DEFAULT FALSE,
    -- FALSE when the model was absent from the ACPI dataset and priced at 0.
    -- Analytics must exclude these rather than report them as free.
    priced               BOOLEAN     NOT NULL DEFAULT FALSE,

    PRIMARY KEY (request_id, "timestamp")
);

SELECT create_hypertable(
    'usage_records', 'timestamp',
    if_not_exists       => TRUE,
    chunk_time_interval => INTERVAL '7 days'
);

CREATE INDEX IF NOT EXISTS usage_records_ws_time_idx
    ON usage_records (workspace_id, "timestamp" DESC);
CREATE INDEX IF NOT EXISTS usage_records_ws_model_time_idx
    ON usage_records (workspace_id, model_id, "timestamp" DESC);
CREATE INDEX IF NOT EXISTS usage_records_ws_feature_time_idx
    ON usage_records (workspace_id, feature_tag, "timestamp" DESC)
    WHERE feature_tag IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Daily rollup — analytics reads this for anything spanning more than a week
-- ---------------------------------------------------------------------------

CREATE MATERIALIZED VIEW IF NOT EXISTS usage_daily
WITH (timescaledb.continuous) AS
SELECT
    time_bucket(INTERVAL '1 day', "timestamp") AS day,
    workspace_id,
    provider,
    model_id,
    feature_tag,
    COUNT(*)                AS requests,
    SUM(input_tokens)       AS input_tokens,
    SUM(output_tokens)      AS output_tokens,
    SUM(cost_usd)           AS cost_usd,
    SUM(acpi_bench_usd)     AS acpi_bench_usd,
    SUM(overpay_usd)        AS overpay_usd
FROM usage_records
WHERE priced
GROUP BY day, workspace_id, provider, model_id, feature_tag
WITH NO DATA;

SELECT add_continuous_aggregate_policy(
    'usage_daily',
    start_offset      => INTERVAL '90 days',
    end_offset        => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 hour',
    if_not_exists     => TRUE
);
