"""Daily v1 prefix-hash cleanup: waits, clears once, then reports done."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import asyncpg

from src import db as dbmod
from src.jobs.prefix_hash_cleanup import clear_plain_prefix_hashes


class FakePool:
    def __init__(self, newest):  # noqa: ANN001
        self.newest = newest
        self.updates: list[str] = []

    async def fetchval(self, query: str):
        if isinstance(self.newest, Exception):
            raise self.newest
        return self.newest

    async def execute(self, query: str) -> str:
        assert "INTERVAL '30 days'" in query and "prefix_hash_version IS NULL" in query
        self.updates.append(query)
        return "UPDATE 3"


def _pass(monkeypatch, newest):  # noqa: ANN001
    pool = FakePool(newest)
    monkeypatch.setattr(dbmod.db, "pool", pool)
    return asyncio.run(clear_plain_prefix_hashes()), pool


def test_none_left_means_done(monkeypatch):
    done, pool = _pass(monkeypatch, None)
    assert done and pool.updates == []


def test_v1_present_runs_the_age_guarded_update(monkeypatch):
    # The 30-day guard lives in the UPDATE itself, so it is safe to send daily.
    done, pool = _pass(monkeypatch, datetime(2026, 9, 1, tzinfo=timezone.utc))
    assert not done and len(pool.updates) == 1


def test_waits_while_the_gateway_has_not_migrated(monkeypatch):
    done, pool = _pass(monkeypatch, asyncpg.UndefinedColumnError("prefix_hash_version"))
    assert not done and pool.updates == []
