"""Tokenizer section counts: off the request path, bounded, marked per row."""

from __future__ import annotations

import dataclasses
import logging
import time

import httpx
from fastapi.testclient import TestClient

from src.routers import proxy
from src.services import anatomy, tokenize
from src.services.anatomy import measure, scale_to_total, split_tokens

from .test_anatomy import SECRET, WS, agent_payload, wait_for_row
from .test_proxy import API_KEY, app  # noqa: F401  (fixture)


def _upstream(prompt_tokens: int = 9000) -> httpx.MockTransport:
    return httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "model": "gpt-4o-mini",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": 40},
            },
        )
    )


def _send(client, payload=None):  # noqa: ANN001
    return client.post(
        "/openai/v1/chat/completions",
        json=payload or {**agent_payload(), "model": "gpt-4o-mini"},
        headers={"authorization": f"Bearer {API_KEY}"},
    )


# --- counting -------------------------------------------------------------------


def test_section_tokens_follow_the_tokenizer_not_the_character_count():
    payload = agent_payload()
    counts = tokenize.section_tokens(payload, "gpt-4o-mini", forwards_tools=True)
    a = measure(payload, WS, forwards_tools=True)
    chars = (a.system_chars, a.tool_chars, a.history_chars, a.user_chars)
    assert all(c > 0 for c in counts)
    # Dense JSON schemas and prose tokenize at different rates, so the two
    # splits disagree; both still sum to the billed total.
    assert scale_to_total(counts, 10_000) != scale_to_total(chars, 10_000)
    assert sum(scale_to_total(counts, 10_000)) == 10_000


def test_dropped_tools_count_zero_and_unknown_models_fall_back():
    payload = agent_payload()
    assert tokenize.section_tokens(payload, "gpt-4o-mini", forwards_tools=False)[1] == 0
    assert tokenize.encoding_for("gpt-9-preview").name == "o200k_base"
    assert tokenize.encoding_for("gpt-4").name == "cl100k_base"


def test_special_token_text_in_content_is_counted_not_rejected():
    payload = {"messages": [{"role": "user", "content": "<|endoftext|> please"}]}
    assert tokenize.section_tokens(payload, "gpt-4o", forwards_tools=True)[3] > 0


def test_anthropic_weights_mark_rows_calibrated(monkeypatch):
    a = measure(agent_payload(), WS, forwards_tools=True)
    assert anatomy.split_method("anthropic") == "estimate"
    assert split_tokens(a, 10_000, "anthropic") == split_tokens(a, 10_000)

    monkeypatch.setattr(anatomy, "ANTHROPIC_SECTION_WEIGHTS", (1.0, 1.3, 1.0, 1.0))
    assert anatomy.split_method("anthropic") == "calibrated"
    assert anatomy.split_method("openai") == "estimate"
    weighted = split_tokens(a, 10_000, "anthropic")
    assert weighted[1] > split_tokens(a, 10_000)[1] and sum(weighted) == 10_000


# --- through the proxy ------------------------------------------------------------


def test_openai_rows_are_tokenized_and_still_sum_to_the_billed_total(app):  # noqa: F811
    app.state.http = httpx.AsyncClient(transport=_upstream())
    with TestClient(app) as client:
        assert _send(client).status_code == 200
        row = wait_for_row(app)
    assert row.split_method == "tokenized"
    assert row.system_tokens + row.tool_tokens + row.history_tokens + row.user_tokens == 9000
    assert row.prefix_cache_saving_usd is not None


def test_full_queue_keeps_the_estimate_and_counts_the_drop(app, monkeypatch):  # noqa: F811
    monkeypatch.setattr(proxy, "settings", dataclasses.replace(proxy.settings, tokenize_max_queued=0))
    monkeypatch.setattr(tokenize, "dropped", 0)
    app.state.http = httpx.AsyncClient(transport=_upstream())
    with TestClient(app) as client:
        _send(client)
        (row,) = app.state.db.rows  # written inline, no task
    assert row.split_method == "estimate" and tokenize.dropped == 1
    assert row.system_tokens + row.tool_tokens + row.history_tokens + row.user_tokens == 9000


def test_tokenizing_adds_nothing_to_the_request_path(app, monkeypatch):  # noqa: F811
    real = tokenize.section_tokens

    def slow(*args, **kwargs):
        time.sleep(0.5)
        return real(*args, **kwargs)

    monkeypatch.setattr(tokenize, "section_tokens", slow)
    app.state.http = httpx.AsyncClient(transport=_upstream())
    with TestClient(app) as client:
        _send(client)  # warm up
        wait_for_row(app)
        app.state.db.rows.clear()

        started = time.perf_counter()
        response = _send(client)
        elapsed = time.perf_counter() - started
        assert response.status_code == 200
        assert elapsed < 0.25, f"request waited {elapsed:.3f}s on tokenizing"
        assert wait_for_row(app).split_method == "tokenized"


def test_failure_keeps_the_estimate_and_logs_no_content(app, monkeypatch, caplog):  # noqa: F811
    def boom(*args, **kwargs):
        raise ValueError(SECRET)

    monkeypatch.setattr(tokenize, "section_tokens", boom)
    app.state.http = httpx.AsyncClient(transport=_upstream())
    with TestClient(app) as client:
        _send(client, {**agent_payload(sentinel=SECRET), "model": "gpt-4o-mini"})
        row = wait_for_row(app)
    assert row.split_method == "estimate" and row.system_tokens is not None
    assert any("tokenizing failed" in r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR)
    assert all(SECRET not in r.getMessage() and not r.exc_info for r in caplog.records)


# --- encodings come from the local cache only ------------------------------------


def test_missing_encodings_never_download_and_never_raise(monkeypatch, tmp_path, caplog):
    import requests
    import tiktoken.load

    def network(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("tried to download an encoding at runtime")

    monkeypatch.setattr(requests, "get", network)
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(tmp_path))  # empty: nothing baked in
    real = tiktoken.load.read_file

    assert tokenize._load_cached("o200k_base") is None
    assert tiktoken.load.read_file is real  # tiktoken left as it was
    assert any("not cached" in r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR)


def test_without_an_encoding_rows_keep_the_estimate(app, monkeypatch):  # noqa: F811
    monkeypatch.setattr(tokenize, "ENCODINGS", {})
    monkeypatch.setattr(tokenize, "dropped", 0)
    assert not tokenize.available("openai", "gpt-4o-mini")
    app.state.http = httpx.AsyncClient(transport=_upstream())
    with TestClient(app) as client:
        assert _send(client).status_code == 200
        (row,) = app.state.db.rows  # written inline, no task queued
    assert row.split_method == "estimate" and tokenize.dropped == 0
    assert row.system_tokens + row.tool_tokens + row.history_tokens + row.user_tokens == 9000
