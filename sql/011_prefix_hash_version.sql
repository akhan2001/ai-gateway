-- Which function produced prefix_hash (gateway services/anatomy.py PREFIX_HASH_VERSION).
--   NULL with a hash: version 1, plain SHA-256 of workspace id + prefix (rows before this column)
--   2: HMAC-SHA256 under a per-workspace key derived from PREFIX_HASH_SECRET
-- Hashes are only ever compared within one version. sql/012 removes the
-- version-1 hashes once they are out of every analysis window.
--
-- Applied by the gateway at startup (services/db.py). Keep in sync with that list.
ALTER TABLE usage_records ADD COLUMN IF NOT EXISTS prefix_hash_version SMALLINT;
