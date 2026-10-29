"""Daily: clear version-1 (plain SHA-256) prefix hashes once they are 30 days old.

Version 1 hashed workspace id + system prompt + tools with no secret, so anyone
holding a candidate prompt could confirm a match. The gateway writes HMACs
(version 2, sql/011) now; this removes the old ones once no 30-day Optimize
window still reads them. sql/012 is the same statement for running by hand.

Waits while the newest version-1 hash is younger than 30 days, clears them all
in one UPDATE once it isn't, and stops once there are none left. Failures (for
example compressed TimescaleDB chunks rejecting the UPDATE) log and retry the
next day.
"""

from __future__ import annotations

import asyncio
import logging

import asyncpg

from ..db import db

log = logging.getLogger(__name__)

_INTERVAL_SECONDS = 86_400
KEEP_DAYS = 30

_NEWEST_V1_SQL = """
SELECT MAX("timestamp") FROM usage_records
WHERE prefix_hash IS NOT NULL AND prefix_hash_version IS NULL
"""
# Re-checks the age inside the statement, so a v1 row written between the
# two queries (a rolled-back gateway) can't be cleared early.
_CLEAR_V1_SQL = f"""
UPDATE usage_records SET prefix_hash = NULL
WHERE prefix_hash IS NOT NULL AND prefix_hash_version IS NULL
  AND ({_NEWEST_V1_SQL}) < NOW() - INTERVAL '{KEEP_DAYS} days'
"""


async def clear_plain_prefix_hashes() -> bool:
    """One pass. True when no version-1 hashes remain."""
    assert db.pool is not None, "database pool not started"
    try:
        newest = await db.pool.fetchval(_NEWEST_V1_SQL)
    except asyncpg.UndefinedColumnError:
        log.info("prefix_hash_version missing: the gateway has not applied sql/011 yet")
        return False
    if newest is None:
        return True
    status = await db.pool.execute(_CLEAR_V1_SQL)
    cleared = int(status.split()[-1]) if status.startswith("UPDATE") else 0
    if cleared:
        log.info("cleared %d version-1 prefix hashes", cleared)
    return False


class PrefixHashCleanup:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None

    async def _run_forever(self) -> None:
        while True:
            try:
                if await clear_plain_prefix_hashes():
                    log.info("no version-1 prefix hashes left; cleanup job done")
                    return
            except Exception:  # never let the daily loop die
                log.exception("prefix hash cleanup failed; retrying tomorrow")
            await asyncio.sleep(_INTERVAL_SECONDS)

    def start(self) -> None:
        self._task = asyncio.create_task(self._run_forever())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None


prefix_hash_cleanup = PrefixHashCleanup()
