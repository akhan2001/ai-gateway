"""Token anatomy, cache-aware cost parts, and the no-content guarantee."""

from __future__ import annotations

import hashlib
import json
import logging
import statistics
import time
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from src.services.acpi import AcpiCatalog
from src.services.anatomy import measure, split_tokens
from src.services.cost import cache_rates, price_request
from src.services.usage import usage_from_obj

from .test_proxy import API_KEY, app  # noqa: F401  (fixture)

WS = uuid.uuid4()
SECRET = "ZZ-SENTINEL-do-not-store-7f3a"


def agent_payload(n_tools: int = 20, n_turns: int = 30, sentinel: str = "x") -> dict:
    tools = [
        {
            "type": "function",
            "function": {
                "name": f"tool_{i}",
                "description": f"Does thing {i}. {sentinel}",
                "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
            },
        }
        for i in range(n_tools)
    ]
    messages = [{"role": "system", "content": f"You are an agent. {sentinel} " * 20}]
    for i in range(n_turns):
        messages.append({"role": "user", "content": f"step {i} {sentinel} " * 40})
        messages.append({"role": "assistant", "content": f"done {i} {sentinel}"})
    messages.append({"role": "user", "content": f"final question {sentinel}"})
    return {"model": "gpt-4o-mini", "messages": messages, "tools": tools, "max_tokens": 500}


# --- anatomy -----------------------------------------------------------------


def test_sections_are_split_correctly():
    a = measure(agent_payload(), WS, forwards_tools=True)
    assert a.tool_count == 20
    assert a.max_output_tokens == 500
    assert min(a.system_chars, a.tool_chars, a.history_chars, a.user_chars) > 0
    assert a.history_chars > a.user_chars


def test_latest_turn_is_everything_after_the_last_assistant_message():
    payload = {
        "messages": [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
            {"role": "tool", "tool_call_id": "c1", "content": "result " * 50},
        ]
    }
    a = measure(payload, WS, forwards_tools=True)
    assert a.user_chars > a.history_chars


def test_dropped_tools_are_not_counted():
    a = measure(agent_payload(), WS, forwards_tools=False)
    assert (a.tool_count, a.tool_chars) == (0, 0)


@pytest.mark.parametrize("total", [0, 1, 7, 999, 123_457])
def test_sections_sum_exactly_to_provider_total(total):
    parts = split_tokens(measure(agent_payload(), WS, forwards_tools=True), total)
    assert sum(parts) == total
    assert all(p >= 0 for p in parts)


def test_prefix_hash_is_stable_salted_and_ignores_the_conversation():
    base = agent_payload()
    longer = agent_payload(n_turns=31)
    assert measure(base, WS, forwards_tools=True).prefix_hash == measure(longer, WS, forwards_tools=True).prefix_hash
    assert measure(base, WS, forwards_tools=True).prefix_hash != measure(base, uuid.uuid4(), forwards_tools=True).prefix_hash
    other_system = json.loads(json.dumps(base))
    other_system["messages"][0]["content"] = "different"
    assert measure(base, WS, forwards_tools=True).prefix_hash != measure(other_system, WS, forwards_tools=True).prefix_hash


def test_prefix_hash_needs_the_secret_and_is_not_the_old_plain_hash(monkeypatch):
    from src.services import anatomy

    payload = agent_payload()
    hashed = measure(payload, WS, forwards_tools=True).prefix_hash
    system = [m for m in payload["messages"] if m["role"] == "system"]
    prefix = json.dumps([system, payload["tools"]], separators=(",", ":"), sort_keys=True, ensure_ascii=False)
    # Version 1, recomputable by anyone holding the prompt:
    assert hashed != hashlib.sha256(f"{WS}:{prefix}".encode()).hexdigest()[:16]
    assert hashed == anatomy.prefix_hash(WS, prefix)

    monkeypatch.setattr(anatomy, "_SECRET", b"another-secret")
    assert anatomy.prefix_hash(WS, prefix) != hashed
    monkeypatch.setattr(anatomy, "_SECRET", b"")
    assert measure(payload, WS, forwards_tools=True).prefix_hash is None


def test_measure_adds_well_under_5ms_p50():
    payload = agent_payload(n_tools=40, n_turns=250)  # ~130 KB request
    assert len(json.dumps(payload)) > 100_000
    timings = []
    for _ in range(50):
        start = time.perf_counter()
        split_tokens(measure(payload, WS, forwards_tools=True), 40_000)
        timings.append(time.perf_counter() - start)
    assert statistics.median(timings) < 0.005


# --- cost, net of caching ----------------------------------------------------

DATASET = {
    "last_updated": "2026-10-06T00:00:00Z",
    "index": {"acpi_usd_per_million": 5.0},
    "models": {
        "anthropic/claude-sonnet-5-5": {"input_per_million": 2.0, "output_per_million": 10.0, "acpi_score": 6.0},
        "openai/gpt-4o": {"input_per_million": 2.5, "output_per_million": 10.0, "acpi_score": 4.1},
    },
}


@pytest.fixture()
def catalog(tmp_path):
    path = tmp_path / "acpi_prices.json"
    path.write_text(json.dumps(DATASET), encoding="utf-8")
    cat = AcpiCatalog(path, refresh_seconds=3600)
    cat.load()
    return cat


def test_anthropic_usage_is_normalized_to_whole_prompt():
    usage = usage_from_obj(
        {"usage": {"input_tokens": 100, "cache_creation_input_tokens": 2000, "cache_read_input_tokens": 8000, "output_tokens": 50}}
    )
    assert (usage.input_tokens, usage.cached_input_tokens, usage.cache_write_tokens, usage.uncached_input_tokens) == (10_100, 8000, 2000, 100)


def test_cost_parts_match_billed_usage_anthropic(catalog):
    # 100 uncached, 2,000 written, 8,000 read, 50 output on Sonnet 5.5 ($2 / $10, reads at 0.05x).
    p = price_request(catalog, "anthropic", "claude-sonnet-5-5", 10_100, 50, cache_read_tokens=8000, cache_write_tokens=2000)
    assert p.uncached_input_cost_usd == pytest.approx(100 * 2 / 1e6)
    assert p.cache_write_cost_usd == pytest.approx(2000 * 2 * 1.25 / 1e6)
    assert p.cache_read_cost_usd == pytest.approx(8000 * 2 * 0.05 / 1e6)
    assert p.output_cost_usd == pytest.approx(50 * 10 / 1e6)
    assert p.cost_usd == pytest.approx(p.uncached_input_cost_usd + p.cache_write_cost_usd + p.cache_read_cost_usd + p.output_cost_usd)


def test_cost_parts_match_billed_usage_openai(catalog):
    # prompt_tokens 5,000 of which 4,000 cached, gpt-4o cached at 50%.
    usage = usage_from_obj({"usage": {"prompt_tokens": 5000, "completion_tokens": 100, "prompt_tokens_details": {"cached_tokens": 4000}}})
    p = price_request(catalog, "openai", "gpt-4o", usage.input_tokens, usage.output_tokens, cache_read_tokens=usage.cached_input_tokens)
    assert p.cost_usd == pytest.approx((1000 * 2.5 + 4000 * 2.5 * 0.5 + 100 * 10) / 1e6)
    assert p.cache_write_cost_usd == 0


def test_no_cache_cost_is_unchanged(catalog):
    assert price_request(catalog, "openai", "gpt-4o", 1_000_000, 1_000_000).cost_usd == pytest.approx(12.5)


@pytest.mark.parametrize(
    ("provider", "model", "read_rate"),
    [
        # Cache read ÷ input, from the provider pages on CACHE_PRICING_AS_OF.
        ("anthropic", "claude-fable-5-1", 0.25 / 10),
        ("anthropic", "claude-opus-5-5", 0.20 / 4),
        ("anthropic", "claude-sonnet-5-5", 0.10 / 2),
        ("anthropic", "claude-opus-5", 0.50 / 5),
        ("anthropic", "claude-sonnet-5", 0.20 / 2),
        ("anthropic", "claude-haiku-5-5", 0.01 / 0.10),
        ("openai", "gpt-6.1-sol", 0.10 / 2),
        ("openai", "gpt-6-sol", 0.20 / 2),
        ("openai", "gpt-5.4-mini", 0.075 / 0.75),
        ("openai", "chat-latest", 0.50 / 5),
        ("openai", "gpt-4.1-mini", 0.10 / 0.40),
        ("openai", "gpt-4o-mini", 0.075 / 0.15),
        ("openai", "o3", 0.50 / 2),
        ("openai", "o3-mini", 0.55 / 1.10),
        ("openai", "o4-mini", 0.275 / 1.10),
        ("google", "gemini-2.5-pro", 0.125 / 1.25),
        ("google", "gemini-3.8-flash", 0.075 / 0.75),
    ],
)
def test_cache_read_rates_match_provider_pricing(provider, model, read_rate):
    assert cache_rates(provider, model)[0] == pytest.approx(read_rate)


def test_cache_write_rates():
    assert cache_rates("anthropic", "claude-opus-5-5")[1] == 1.25  # $5 write on $4 input
    assert cache_rates("openai", "gpt-5")[1] == 1.0
    assert cache_rates("unknown", "m") == (1.0, 1.0)


# --- the hard rule: no content anywhere --------------------------------------


def test_no_prompt_or_completion_text_reaches_the_ledger_or_logs(app, caplog):  # noqa: F811
    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "gpt-4o-mini",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": f"answer {SECRET}"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 9000, "completion_tokens": 40, "prompt_tokens_details": {"cached_tokens": 6000}},
            },
        )

    app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    caplog.set_level(logging.DEBUG)
    with TestClient(app) as client:
        response = client.post(
            "/openai/v1/chat/completions",
            json=agent_payload(sentinel=SECRET),
            headers={"authorization": f"Bearer {API_KEY}"},
        )
    assert response.status_code == 200

    (row,) = app.state.db.rows
    assert SECRET not in repr(row.as_tuple())
    assert all(SECRET not in record.getMessage() for record in caplog.records)
    # ...while the anatomy itself was captured and adds up.
    assert row.system_tokens + row.tool_tokens + row.history_tokens + row.user_tokens == 9000
    assert (row.tool_count, row.uncached_input_tokens, row.cached_input_tokens) == (20, 3000, 6000)
    assert row.prefix_hash and len(row.prefix_hash) == 16
    assert row.prefix_hash_version == 2


def test_error_rows_carry_no_anatomy(app):  # noqa: F811
    app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(429, json={"error": "slow down"})))
    with TestClient(app) as client:
        client.post("/openai/v1/chat/completions", json=agent_payload(), headers={"authorization": f"Bearer {API_KEY}"})
    (row,) = app.state.db.rows
    assert row.system_tokens is None and row.prefix_hash is None and row.prefix_hash_version is None


# --- startup migration -------------------------------------------------------


class FakeConn:
    def __init__(self, present: set[str]) -> None:
        self.present = present
        self.executed: list[str] = []

    async def fetch(self, query: str):
        return [{"column_name": c} for c in self.present]

    def transaction(self):
        conn = self

        class Tx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return Tx()

    async def execute(self, statement: str) -> None:
        self.executed.append(statement)


@pytest.mark.asyncio
async def test_migration_takes_no_lock_when_columns_exist():
    from src.services.db import _LEDGER_MIGRATION_COLUMNS, _apply_ledger_migration

    conn = FakeConn({name for name, _ in _LEDGER_MIGRATION_COLUMNS})
    await _apply_ledger_migration(conn)
    assert conn.executed == []


@pytest.mark.asyncio
async def test_migration_adds_only_missing_columns_under_a_lock_timeout():
    from src.services.db import _LEDGER_MIGRATION_COLUMNS, _apply_ledger_migration

    conn = FakeConn({name for name, _ in _LEDGER_MIGRATION_COLUMNS[1:]})
    await _apply_ledger_migration(conn)
    assert conn.executed == [
        "SET LOCAL lock_timeout = '5s'",
        "ALTER TABLE usage_records ADD COLUMN IF NOT EXISTS system_tokens INTEGER",
    ]
