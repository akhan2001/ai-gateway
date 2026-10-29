"""Postgres/TimescaleDB access and the batched ledger writer.

Ledger writes are queued and flushed by a background task. Enqueue is
non-blocking and *drops* on overflow rather than applying backpressure: losing
a ledger row is strictly better than slowing down a customer's inference
request.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import asyncpg

from ..models.log import LEDGER_COLUMNS, UsageRecord

log = logging.getLogger(__name__)

# Ledger migrations, embedded because the gateway image doesn't ship sql/.
# Run before the writer starts: the COPY names these columns.
_LEDGER_MIGRATION_COLUMNS = (
    # sql/008_token_anatomy.sql
    ("system_tokens", "INTEGER"),
    ("tool_tokens", "INTEGER"),
    ("history_tokens", "INTEGER"),
    ("user_tokens", "INTEGER"),
    ("tool_count", "INTEGER"),
    ("cache_write_tokens", "BIGINT"),
    ("uncached_input_tokens", "BIGINT"),
    ("max_output_tokens", "INTEGER"),
    ("prefix_hash", "TEXT"),
    ("uncached_input_cost_usd", "NUMERIC(16, 8)"),
    ("cache_write_cost_usd", "NUMERIC(16, 8)"),
    ("cache_read_cost_usd", "NUMERIC(16, 8)"),
    ("output_cost_usd", "NUMERIC(16, 8)"),
    ("prefix_cache_saving_usd", "NUMERIC(16, 8)"),
    # sql/010_cost_method_version.sql
    ("cost_method_version", "SMALLINT"),
    # sql/011_prefix_hash_version.sql
    ("prefix_hash_version", "SMALLINT"),
    # sql/013_split_method.sql
    ("split_method", "TEXT"),
)

# sql/009_compression_shadow.sql.
_COLUMNS_009 = (
    ("usage_records", "shadow_compressed_tokens", "BIGINT"),
    ("usage_records", "shadow_saving_tokens", "BIGINT"),
    ("usage_records", "shadow_saving_usd", "NUMERIC(16, 8)"),
    ("usage_records", "shadow_prefix_changed", "BOOLEAN"),
    ("usage_records", "shadow_sample_rate", "REAL"),
    ("workspaces", "compression_shadow", "BOOLEAN NOT NULL DEFAULT FALSE"),
)

# Everything _apply_ledger_migration adds, as (table, column, type). Order is
# cosmetic: every column is nullable or has a constant default.
_MIGRATION_COLUMNS = tuple(("usage_records", name, kind) for name, kind in _LEDGER_MIGRATION_COLUMNS) + _COLUMNS_009


async def _ledger_columns_present(conn: asyncpg.Connection) -> set[str]:
    return {
        row["column_name"]
        for row in await conn.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'usage_records'"
        )
    }


async def _apply_ledger_migration(conn: asyncpg.Connection) -> None:
    """Add any missing ledger columns (008, 009, 010, 011, 013).

    Checks first, because even ``ADD COLUMN IF NOT EXISTS`` takes an ACCESS
    EXCLUSIVE lock on the hypertable; doing that on every restart would stall
    ledger inserts and analytics reads behind it. When columns are missing, a
    short lock_timeout keeps a long-running analytics query from making the
    ALTER queue every insert behind it: on timeout this fails, logs, and the
    next restart retries. Concurrent replicas are safe: Postgres serializes the
    ALTERs on that lock and IF NOT EXISTS makes the loser a no-op.
    """
    present = {
        (row["table_name"], row["column_name"])
        for row in await conn.fetch(
            "SELECT table_name, column_name FROM information_schema.columns"
            " WHERE table_name IN ('usage_records', 'workspaces')"
        )
    }
    missing = [(table, name, kind) for table, name, kind in _MIGRATION_COLUMNS if (table, name) not in present]
    if not missing:
        return
    async with conn.transaction():
        await conn.execute("SET LOCAL lock_timeout = '5s'")
        for table, name, kind in missing:
            await conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {kind}")
    log.info("ledger migrations added %d columns", len(missing))


@dataclass
class WriterStats:
    enqueued: int = 0
    written: int = 0
    dropped: int = 0
    failed: int = 0
    #: Ledger columns absent from the table (a migration failed): rows are
    #: written without them. Should be 0; anything else needs a fix and a restart.
    missing_columns: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "enqueued": self.enqueued,
            "written": self.written,
            "dropped": self.dropped,
            "failed": self.failed,
            "missing_columns": self.missing_columns,
        }


class Database:
    """Owns the connection pool and the write queue."""

    def __init__(
        self,
        dsn: str,
        *,
        queue_capacity: int = 8192,
        batch_size: int = 256,
        flush_seconds: float = 2.0,
    ) -> None:
        self._dsn = dsn
        self._queue_capacity = queue_capacity
        self._batch_size = batch_size
        self._flush_seconds = flush_seconds
        self.pool: asyncpg.Pool | None = None
        self.stats = WriterStats()
        self._queue: asyncio.Queue[UsageRecord] | None = None
        self._task: asyncio.Task[None] | None = None
        #: What the COPY names: LEDGER_COLUMNS minus any the table lacks.
        self._columns: tuple[str, ...] = LEDGER_COLUMNS

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        self.pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=10)
        # A failed migration must not stop the proxy, and must not lose rows:
        # the writer falls back to the columns the table actually has, so only
        # the new fields go missing until it's fixed (see /health).
        async with self.pool.acquire() as conn:
            try:
                await _apply_ledger_migration(conn)
            except Exception:
                log.exception("ledger migration failed")
            try:
                present = await _ledger_columns_present(conn)
            except Exception:
                log.exception("could not read usage_records columns; writing all")
                present = set(LEDGER_COLUMNS)
        self._columns = tuple(c for c in LEDGER_COLUMNS if c in present)
        missing = [c for c in LEDGER_COLUMNS if c not in present]
        self.stats.missing_columns = len(missing)
        if missing:
            log.error("usage_records lacks %d ledger columns; writing rows without: %s", len(missing), ", ".join(missing))
        self._queue = asyncio.Queue(maxsize=self._queue_capacity)
        self._task = asyncio.create_task(self._drain_forever())
        log.info("database pool up, ledger writer started")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        # Best-effort flush of whatever is still queued at shutdown.
        if self._queue is not None:
            remaining: list[UsageRecord] = []
            while not self._queue.empty():
                remaining.append(self._queue.get_nowait())
            if remaining:
                await self._write_batch(remaining)
            self._queue = None

        if self.pool is not None:
            await self.pool.close()
            self.pool = None
        log.info("database stopped: %s", self.stats.as_dict())

    # -- ledger -------------------------------------------------------------

    def enqueue(self, record: UsageRecord) -> bool:
        """Queue a ledger row. Returns False if it was dropped."""
        if self._queue is None:
            self.stats.dropped += 1
            return False
        try:
            self._queue.put_nowait(record)
        except asyncio.QueueFull:
            self.stats.dropped += 1
            log.warning("ledger queue full; dropped request %s", record.request_id)
            return False
        self.stats.enqueued += 1
        return True

    async def _drain_forever(self) -> None:
        assert self._queue is not None
        loop = asyncio.get_running_loop()
        while True:
            batch: list[UsageRecord] = []
            try:
                # Block on the first row so an idle gateway does no work.
                batch.append(await self._queue.get())
                deadline = loop.time() + self._flush_seconds
                while len(batch) < self._batch_size:
                    timeout = deadline - loop.time()
                    if timeout <= 0:
                        break
                    try:
                        batch.append(await asyncio.wait_for(self._queue.get(), timeout))
                    except asyncio.TimeoutError:
                        break
            except asyncio.CancelledError:
                if batch:
                    await self._write_batch(batch)
                raise

            await self._write_batch(batch)

    async def _write_batch(self, batch: list[UsageRecord]) -> None:
        if not batch or self.pool is None:
            return
        records = [tuple(getattr(row, c) for c in self._columns) for row in batch]
        try:
            async with self.pool.acquire() as conn:
                await conn.copy_records_to_table(
                    "usage_records", records=records, columns=list(self._columns)
                )
            self.stats.written += len(records)
        except Exception:
            self.stats.failed += len(records)
            log.exception("failed to write %d ledger rows", len(records))

    # -- lookups ------------------------------------------------------------

    async def resolve_api_key(self, key_hash: str) -> dict[str, Any] | None:
        """Workspace behind a key hash, or None if unknown/revoked/disabled."""
        if self.pool is None:
            return None
        row = await self.pool.fetchrow(
            """
            SELECT k.id AS key_id, k.workspace_id, w.name AS workspace_name,
                   -- Read via to_jsonb so a missing column (009 not applied
                   -- yet) means "off" instead of failing every key lookup.
                   COALESCE((to_jsonb(w) ->> 'compression_shadow')::boolean, FALSE)
                       AS compression_shadow
            FROM api_keys k
            JOIN workspaces w ON w.id = k.workspace_id
            WHERE k.key_hash = $1 AND NOT k.revoked AND w.enabled
            """,
            key_hash,
        )
        return dict(row) if row else None

    async def provider_credential(
        self, workspace_id: UUID, provider: str
    ) -> tuple[str, dict[str, Any] | None] | None:
        """Encrypted key plus provider-specific config (e.g. Azure's resource
        name and deployment map), or None if nothing is stored."""
        if self.pool is None:
            return None
        row = await self.pool.fetchrow(
            "SELECT encrypted_key, config FROM provider_keys WHERE workspace_id = $1 AND provider = $2",
            workspace_id,
            provider,
        )
        if row is None:
            return None
        config = row["config"]
        return row["encrypted_key"], (json.loads(config) if isinstance(config, str) else config)

    async def touch_key(self, key_id: UUID) -> None:
        """Record last use. Fire-and-forget; failure must not affect the request."""
        if self.pool is None:
            return
        try:
            await self.pool.execute(
                "UPDATE api_keys SET last_used_at = NOW() WHERE id = $1", key_id
            )
        except Exception:
            log.debug("failed to update last_used_at for key %s", key_id, exc_info=True)
