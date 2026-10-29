"""Ledger writer: a failed migration loses the new fields, never the row."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import asyncpg

from src.models.log import LEDGER_COLUMNS, UsageRecord
from src.services import db as dbmod
from src.services.db import Database

# The table as it stood before 008: everything up to the anatomy columns.
_PRE_008 = set(LEDGER_COLUMNS[: LEDGER_COLUMNS.index("system_tokens")])


class FakeConn:
    def __init__(self, columns: set[str], migration_fails: bool) -> None:
        self.columns = columns
        self.migration_fails = migration_fails
        self.copies: list[tuple[list[str], list[tuple]]] = []

    async def fetch(self, query: str, *args):  # noqa: ANN001
        return [{"column_name": c} for c in self.columns]

    async def execute(self, query: str, *args):  # noqa: ANN001
        if self.migration_fails and query.startswith("ALTER"):
            raise asyncpg.LockNotAvailableError("lock timeout")

    def transaction(self):
        @asynccontextmanager
        async def tx():
            yield

        return tx()

    async def copy_records_to_table(self, table: str, *, records, columns):  # noqa: ANN001
        assert set(columns) <= self.columns, "COPY named a column the table lacks"
        self.copies.append((columns, list(records)))


class FakePool:
    def __init__(self, conn: FakeConn) -> None:
        self.conn = conn

    def acquire(self):
        @asynccontextmanager
        async def cm():
            yield self.conn

        return cm()

    async def close(self) -> None:
        pass


def _record() -> UsageRecord:
    return UsageRecord(
        request_id=uuid.uuid4(),
        timestamp=datetime.now(timezone.utc),
        workspace_id=uuid.uuid4(),
        provider="openai",
        model_id="gpt-4o",
        feature_tag=None,
        workload_tag=None,
        session_id=None,
        step_name=None,
        session_name=None,
        session_status=None,
        auto_generated=False,
        input_tokens=100,
        output_tokens=10,
        cached_input_tokens=0,
        cost_usd=0.01,
        acpi_bench_usd=0.0,
        overpay_usd=0.0,
        acpi_score=None,
        acpi_dataset_version=None,
        latency_ms=5,
        status_code=200,
        is_stream=False,
        priced=True,
        system_tokens=40,
    )


async def _run(monkeypatch, conn: FakeConn) -> Database:  # noqa: ANN001
    async def create_pool(*args, **kwargs):  # noqa: ANN001
        return FakePool(conn)

    monkeypatch.setattr(dbmod.asyncpg, "create_pool", create_pool)
    database = Database("postgresql://fake", flush_seconds=0.01)
    await database.start()
    database.enqueue(_record())
    await asyncio.sleep(0.05)
    await database.stop()
    return database


def test_failed_migration_still_writes_the_row_without_the_new_columns(monkeypatch):
    conn = FakeConn(set(_PRE_008), migration_fails=True)
    database = asyncio.run(_run(monkeypatch, conn))

    (columns, records), = conn.copies
    assert "system_tokens" not in columns and "cost_usd" in columns
    assert dict(zip(columns, records[0]))["cost_usd"] == 0.01
    assert database.stats.written == 1 and database.stats.failed == 0
    assert database.stats.missing_columns == len(LEDGER_COLUMNS) - len(_PRE_008)


def test_migrated_table_gets_every_column(monkeypatch):
    conn = FakeConn(set(LEDGER_COLUMNS), migration_fails=False)
    database = asyncio.run(_run(monkeypatch, conn))

    (columns, records), = conn.copies
    assert tuple(columns) == LEDGER_COLUMNS
    assert dict(zip(columns, records[0]))["system_tokens"] == 40
    assert database.stats.missing_columns == 0
