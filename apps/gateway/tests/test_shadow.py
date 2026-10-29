"""Compression shadow mode: prefix-stable transforms, net-of-cache savings,
no content stored, and nothing added to the request path."""

from __future__ import annotations

import copy
import dataclasses
import json
import logging
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from src.middleware.auth import Principal
from src.routers import proxy
from src.services import shadow
from src.services.cost import input_saving_usd
from src.services.shadow import compress_messages, estimate

from .test_anatomy import SECRET, catalog  # noqa: F401  (fixture)
from .test_proxy import API_KEY, FakeAuth, app  # noqa: F401  (fixture)


def agent_conversation(turns: int = 6, sentinel: str = "x") -> list[dict]:
    """OpenAI-shaped agent loop: pretty-printed JSON tool results, a tool result
    the agent fetches twice, and sloppy whitespace."""
    catalog_result = json.dumps({"items": [{"sku": f"A-{i}", "note": sentinel} for i in range(20)]}, indent=2)
    messages: list[dict] = [{"role": "system", "content": f"You are an agent.   \n\n\n\n{sentinel}"}]
    for i in range(turns):
        messages.append({"role": "user", "content": [{"type": "text", "text": f"step {i} {sentinel}   \n\n\n\nplease"}]})
        messages.append({"role": "assistant", "content": None, "tool_calls": [{"id": f"call_{i}", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}]})
        messages.append({"role": "tool", "tool_call_id": f"call_{i}", "content": catalog_result})
        messages.append({"role": "assistant", "content": f"found it {i}  \n\n\n\n"})
    return messages


# --- transforms (ported from demos/compression-lab/lib/lab.test.ts) -----------


def test_compression_is_prefix_stable():
    full_input = agent_conversation()
    full = compress_messages(full_input)
    for n in range(1, len(full_input)):
        assert compress_messages(full_input[:n]) == full[:n], f"prefix {n}"


def test_compression_is_deterministic_and_never_mutates_its_input():
    messages = agent_conversation()
    before = copy.deepcopy(messages)
    assert compress_messages(messages) == compress_messages(messages)
    assert messages == before


def test_compression_shrinks_and_keeps_tool_call_pairing_and_system():
    messages = agent_conversation()
    out = compress_messages(messages)
    assert len(json.dumps(out)) < len(json.dumps(messages))
    ids = lambda ms: [(m.get("tool_call_id"), [c["id"] for c in m.get("tool_calls") or []]) for m in ms]  # noqa: E731
    assert ids(out) == ids(messages)
    assert out[0] == messages[0]  # system untouched


def test_only_later_duplicates_become_references():
    block = "x" * 300
    out = compress_messages([
        {"role": "user", "content": block},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": block},
    ], dedupe_references=True)
    assert out[0]["content"] == block
    assert out[2]["content"] == "[Duplicate omitted: identical to content in message 1.]"


def test_dedupe_is_off_by_default():
    messages = agent_conversation()
    assert "Duplicate omitted" not in json.dumps(compress_messages(messages))
    on = compress_messages(messages, dedupe_references=True)
    assert "Duplicate omitted" in json.dumps(on)
    for n in range(1, len(messages)):  # prefix-stable with it on, too
        assert compress_messages(messages[:n], dedupe_references=True) == on[:n]


def test_json_tool_results_are_minified_other_text_only_whitespace_normalized():
    out = compress_messages([
        {"role": "tool", "tool_call_id": "t", "content": '{\n  "a": 1\n}'},
        {"role": "user", "content": [{"type": "text", "text": "keep  inner\t\n\n\n\nspaces   "}]},
        {"role": "user", "content": '{\n  "not": "a tool result"\n}'},
    ])
    assert out[0]["content"] == '{"a":1}'
    assert out[1]["content"] == [{"type": "text", "text": "keep  inner\n\nspaces"}]
    assert out[2]["content"] == '{\n  "not": "a tool result"\n}'


# --- token estimate -----------------------------------------------------------


def test_estimate_scales_to_the_provider_total():
    s = estimate({"messages": agent_conversation()}, 10_000, 0, forwards_tools=True)
    assert s.saving_tokens > 0
    assert s.compressed_tokens + s.saving_tokens == 10_000
    assert s.saving_tokens == round(sum(saved for _, _, saved in s.spans))
    assert not s.touches_cached_prefix


def test_cached_prefix_flag():
    payload = {"messages": agent_conversation()}
    assert estimate(payload, 10_000, 9_000, forwards_tools=True).touches_cached_prefix
    # A cache covering only the untouched system prompt isn't affected.
    assert not estimate(payload, 10_000, 1, forwards_tools=True).touches_cached_prefix


def test_nothing_to_compress():
    assert estimate({"messages": [{"role": "user", "content": "hi"}]}, 10, 0, forwards_tools=True).saving_tokens == 0
    assert estimate({}, 10, 0, forwards_tools=True) is None


# --- net of caching -------------------------------------------------------------


def test_saving_priced_at_the_rate_each_position_was_billed(catalog):  # noqa: F811
    # gpt-4o: $2.50/M input, cached reads at 0.5x. 1,000 tokens saved from a
    # span half inside the 5,000-token cached prefix.
    usd = input_saving_usd(catalog, "openai", "gpt-4o", ((4_000, 6_000, 1_000),), cache_read_tokens=5_000, cache_write_tokens=0)
    assert usd == pytest.approx((500 * 0.5 + 500 * 1.0) * 2.5 / 1e6)


def test_saving_on_anthropic_reads_writes_and_uncached(catalog):  # noqa: F811
    # Sonnet 5.5: $2/M, read 0.05x, write 1.25x. Three spans, one per band.
    spans = ((0, 100, 10), (1_000, 1_100, 10), (2_000, 2_100, 10))
    usd = input_saving_usd(catalog, "anthropic", "claude-sonnet-5-5", spans, cache_read_tokens=1_000, cache_write_tokens=1_000)
    assert usd == pytest.approx(10 * (0.05 + 1.25 + 1.0) * 2 / 1e6)


def test_no_cache_saving_is_full_input_price(catalog):  # noqa: F811
    assert input_saving_usd(catalog, "openai", "gpt-4o", ((0, 10, 4),), cache_read_tokens=0, cache_write_tokens=0) == pytest.approx(4 * 2.5 / 1e6)
    assert input_saving_usd(catalog, "openai", "unknown-model", ((0, 10, 4),), cache_read_tokens=0, cache_write_tokens=0) is None


# --- through the proxy ------------------------------------------------------------


@pytest.fixture(autouse=True)
def sample_everything(monkeypatch):
    """Proxy tests measure every request; sampling has its own test."""
    monkeypatch.setattr(proxy, "settings", dataclasses.replace(proxy.settings, shadow_sample_rate=1.0))


class ShadowAuth(FakeAuth):
    async def authenticate(self, authorization):  # noqa: ANN001
        principal = await super().authenticate(authorization)
        return principal and Principal(principal.workspace_id, principal.workspace_name, principal.key_id, compression_shadow=True)


def _upstream(prompt_tokens: int = 9000, cached: int = 0) -> httpx.MockTransport:
    return httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "model": "gpt-4o-mini",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": f"answer {SECRET}"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": 40, "prompt_tokens_details": {"cached_tokens": cached}},
            },
        )
    )


def _send(app, client) -> httpx.Response:  # noqa: F811
    return client.post(
        "/openai/v1/chat/completions",
        json={"model": "gpt-4o-mini", "messages": agent_conversation(sentinel=SECRET)},
        headers={"authorization": f"Bearer {API_KEY}"},
    )


def _wait_for_row(app):  # noqa: F811
    deadline = time.monotonic() + 5
    while not app.state.db.rows and time.monotonic() < deadline:
        time.sleep(0.01)
    (row,) = app.state.db.rows
    return row


def test_shadow_row_carries_counts_only(app, caplog):  # noqa: F811
    app.state.auth = ShadowAuth(API_KEY)
    app.state.http = httpx.AsyncClient(transport=_upstream(cached=2000))
    caplog.set_level(logging.DEBUG)
    with TestClient(app) as client:
        assert _send(app, client).status_code == 200
        row = _wait_for_row(app)

    assert SECRET not in repr(row.as_tuple())
    assert all(SECRET not in record.getMessage() for record in caplog.records)
    assert row.shadow_saving_tokens > 0
    assert row.shadow_compressed_tokens + row.shadow_saving_tokens == 9000
    assert row.shadow_saving_usd > 0
    assert isinstance(row.shadow_prefix_changed, bool)


def test_shadow_is_off_unless_the_workspace_opts_in(app):  # noqa: F811
    app.state.http = httpx.AsyncClient(transport=_upstream())
    with TestClient(app) as client:
        _send(app, client)
    (row,) = app.state.db.rows
    assert row.system_tokens is not None
    assert (row.shadow_saving_tokens, row.shadow_saving_usd, row.shadow_prefix_changed) == (None, None, None)


def test_shadow_adds_nothing_to_the_request_path(app, monkeypatch):  # noqa: F811
    real = shadow.estimate

    def slow(*args, **kwargs):
        time.sleep(0.5)
        return real(*args, **kwargs)

    monkeypatch.setattr(shadow, "estimate", slow)
    app.state.auth = ShadowAuth(API_KEY)
    app.state.http = httpx.AsyncClient(transport=_upstream())
    with TestClient(app) as client:
        _send(app, client)  # warm up
        _wait_for_row(app)
        app.state.db.rows.clear()

        started = time.perf_counter()
        response = _send(app, client)
        elapsed = time.perf_counter() - started
        assert response.status_code == 200
        assert elapsed < 0.25, f"request waited {elapsed:.3f}s on shadow work"
        assert app.state.db.rows == []  # still computing...
        assert _wait_for_row(app).shadow_saving_tokens > 0  # ...and lands after


def test_shadow_failure_still_writes_the_row_and_logs_no_content(app, monkeypatch, caplog):  # noqa: F811
    def boom(*args, **kwargs):
        raise ValueError(SECRET)

    monkeypatch.setattr(shadow, "estimate", boom)
    app.state.auth = ShadowAuth(API_KEY)
    app.state.http = httpx.AsyncClient(transport=_upstream())
    with TestClient(app) as client:
        _send(app, client)
        row = _wait_for_row(app)
    assert row.cost_usd > 0 and row.shadow_saving_tokens is None
    assert any("compression shadow failed" in r.getMessage() for r in caplog.records)
    assert all(SECRET not in r.getMessage() and not r.exc_info for r in caplog.records)



def test_kill_switch_stops_shadow_work(app, monkeypatch):  # noqa: F811
    monkeypatch.setattr(shadow, "killed", True)
    app.state.auth = ShadowAuth(API_KEY)
    app.state.http = httpx.AsyncClient(transport=_upstream())
    with TestClient(app) as client:
        _send(app, client)
    (row,) = app.state.db.rows  # written inline, no shadow task
    assert row.cost_usd > 0 and row.shadow_saving_tokens is None


def test_inflight_cap_writes_the_row_without_an_estimate_and_counts_it(app, monkeypatch):  # noqa: F811
    monkeypatch.setattr(proxy, "settings", dataclasses.replace(proxy.settings, shadow_max_inflight=0))
    monkeypatch.setattr(shadow, "dropped", 0)
    app.state.auth = ShadowAuth(API_KEY)
    app.state.http = httpx.AsyncClient(transport=_upstream())
    with TestClient(app) as client:
        _send(app, client)
    (row,) = app.state.db.rows
    assert row.cost_usd > 0 and row.shadow_saving_tokens is None
    assert shadow.dropped == 1


def test_unsampled_requests_get_no_estimate_and_sampled_rows_carry_the_rate(app, monkeypatch):  # noqa: F811
    app.state.auth = ShadowAuth(API_KEY)
    app.state.http = httpx.AsyncClient(transport=_upstream())
    monkeypatch.setattr(proxy, "settings", dataclasses.replace(proxy.settings, shadow_sample_rate=0.0))
    with TestClient(app) as client:
        _send(app, client)
        row = _wait_for_row(app)  # still lands after the response: it is tokenized
        assert row.shadow_saving_tokens is None and row.shadow_sample_rate is None
        assert row.split_method == "tokenized"
        app.state.db.rows.clear()

        monkeypatch.setattr(proxy, "settings", dataclasses.replace(proxy.settings, shadow_sample_rate=0.5))
        monkeypatch.setattr(proxy.random, "random", lambda: 0.49)
        _send(app, client)
        row = _wait_for_row(app)
    assert row.shadow_saving_tokens > 0 and row.shadow_sample_rate == 0.5


# --- shared with the tokenizer (services/tokenize) -------------------------------


def test_a_sampled_openai_row_gets_both_tokenizer_counts_and_a_shadow_estimate(app):  # noqa: F811
    app.state.auth = ShadowAuth(API_KEY)
    app.state.http = httpx.AsyncClient(transport=_upstream())
    with TestClient(app) as client:
        _send(app, client)
        row = _wait_for_row(app)
    assert row.split_method == "tokenized" and row.shadow_saving_tokens > 0
    assert row.system_tokens + row.tool_tokens + row.history_tokens + row.user_tokens == 9000


def test_a_full_queue_drops_both_and_still_writes_the_row(app, monkeypatch):  # noqa: F811
    from src.services import tokenize

    monkeypatch.setattr(proxy, "settings", dataclasses.replace(proxy.settings, shadow_sample_rate=1.0, tokenize_max_queued=0))
    monkeypatch.setattr(shadow, "dropped", 0)
    monkeypatch.setattr(tokenize, "dropped", 0)
    app.state.auth = ShadowAuth(API_KEY)
    app.state.http = httpx.AsyncClient(transport=_upstream())
    with TestClient(app) as client:
        _send(app, client)
        (row,) = app.state.db.rows  # inline: nothing could be queued
    assert row.split_method == "estimate" and row.shadow_saving_tokens is None
    assert (shadow.dropped, tokenize.dropped) == (1, 1)
