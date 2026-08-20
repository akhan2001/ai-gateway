"""Postgres/TimescaleDB access and the batched ledger writer.

Ledger writes are queued and flushed by a background task. Enqueue is
non-blocking and *drops* on overflow rather than applying backpressure: losing
a ledger row is strictly better than slowing down a customer's inference
request.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import asyncpg

from ..models.log import LEDGER_COLUMNS, UsageRecord

log = logging.getLogger(__name__)


@dataclass
class WriterStats:
    enqueued: int = 0
    written: int = 0
    dropped: int = 0
    failed: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "enqueued": self.enqueued,
            "written": self.written,
            "dropped": self.dropped,
            "failed": self.failed,
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

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        self.pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=10)
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
        records = [row.as_tuple() for row in batch]
        try:
            async with self.pool.acquire() as conn:
                await conn.copy_records_to_table(
                    "usage_records", records=records, columns=list(LEDGER_COLUMNS)
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
            SELECT k.id AS key_id, k.workspace_id, w.name AS workspace_name
            FROM api_keys k
            JOIN workspaces w ON w.id = k.workspace_id
            WHERE k.key_hash = $1 AND NOT k.revoked AND w.enabled
            """,
            key_hash,
        )
        return dict(row) if row else None

    async def provider_key(self, workspace_id: UUID, provider: str) -> str | None:
        """Encrypted provider credential for a workspace."""
        if self.pool is None:
            return None
        return await self.pool.fetchval(
            "SELECT encrypted_key FROM provider_keys WHERE workspace_id = $1 AND provider = $2",
            workspace_id,
            provider,
        )

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
