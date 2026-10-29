-- Scheduled: the analytics API runs the same check and UPDATE daily
-- (src/jobs/prefix_hash_cleanup.py) and stops once none are left. This file is
-- the by-hand equivalent. Either way nothing is cleared until the newest
-- version-1 prefix hash is more than 30 days old (the Optimize window), so no
-- recommendation still reads them.
--
-- Version-1 hashes are a plain SHA-256 of workspace id + system prompt + tools.
-- Anyone with a candidate prompt and the workspace id can recompute one and
-- confirm a match, which is why version 2 is an HMAC. Keeping the old ones
-- would leave that open, so they are cleared, not kept for history.
--
-- Refuses to run early. The UPDATE touches only rows from before sql/011.
-- If old chunks are TimescaleDB-compressed, decompress them first or the
-- UPDATE fails (nothing is changed in that case).
DO $$
DECLARE newest timestamptz;
BEGIN
    SELECT MAX("timestamp") INTO newest
    FROM usage_records
    WHERE prefix_hash IS NOT NULL AND prefix_hash_version IS NULL;

    IF newest IS NULL THEN
        RAISE NOTICE 'No version-1 prefix hashes left.';
    ELSIF newest > NOW() - INTERVAL '30 days' THEN
        RAISE EXCEPTION 'Newest version-1 prefix hash is from %; wait until 30 days after it.', newest;
    ELSE
        UPDATE usage_records SET prefix_hash = NULL
        WHERE prefix_hash IS NOT NULL AND prefix_hash_version IS NULL;
        RAISE NOTICE 'Cleared version-1 prefix hashes.';
    END IF;
END $$;
