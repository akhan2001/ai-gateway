"""Read-only Postgres access for the analytics API."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import asyncpg

log = logging.getLogger(__name__)


def dsn() -> str:
    raw = os.getenv("DATABASE_URL", "postgresql://tokenix:tokenix@localhost:5432/tokenix")
    scheme, _, rest = raw.partition("://")
    if "+" in scheme:
        return f"{scheme.split('+', 1)[0]}://{rest}"
    return raw


class Database:
    def __init__(self) -> None:
        self.pool: asyncpg.Pool | None = None

    async def start(self) -> None:
        self.pool = await asyncpg.create_pool(dsn(), min_size=1, max_size=10)
        log.info("analytics database pool up")

    async def stop(self) -> None:
        if self.pool is not None:
            await self.pool.close()
            self.pool = None

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        assert self.pool is not None, "database pool not started"
        rows = await self.pool.fetch(query, *args)
        return [dict(row) for row in rows]

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        assert self.pool is not None, "database pool not started"
        row = await self.pool.fetchrow(query, *args)
        return dict(row) if row else None

    async def iterate(
        self, query: str, *args: Any, prefetch: int = 1_000
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream rows through a server-side cursor.

        The dashboard endpoints all aggregate, so their result sets are bounded
        by the number of models or days. A raw CSV export is bounded only by
        how much traffic the workspace pushed, so it reads through a cursor and
        keeps memory flat instead of materialising the whole month.
        """
        assert self.pool is not None, "database pool not started"
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                async for row in conn.cursor(query, *args, prefetch=prefetch):
                    yield dict(row)


db = Database()


def f(value: Any) -> float:
    """Coerce a NUMERIC/Decimal/None to a float for JSON."""
    return float(value) if value is not None else 0.0
