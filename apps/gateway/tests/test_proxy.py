"""End-to-end proxy tests with a stubbed upstream and an in-memory ledger.

This is the test the MVP is judged by: a request goes in, a correct response
comes out, and exactly one priced row lands in the ledger.
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.middleware.auth import Principal, generate_key
from src.models.log import UsageRecord
from src.routers import proxy
from src.services.acpi import AcpiCatalog

DATASET = {
    "last_updated": "2026-08-18T17:23:01Z",
    "index": {"acpi_usd_per_million": 5.0},
    "models": {
        "openai/gpt-4o-mini": {
            "input_per_million": 0.15,
            "output_per_million": 0.6,
            "acpi_score": 6.9,
        },
        "anthropic/claude-haiku-4-5": {
            "input_per_million": 1.0,
            "output_per_million": 5.0,
            "acpi_score": 5.8,
        },
    },
}

WORKSPACE_ID = uuid.uuid4()
API_KEY = generate_key()


class FakeDatabase:
    """Captures ledger rows instead of writing them."""

    def __init__(self) -> None:
        self.rows: list[UsageRecord] = []
        self.pool = None

    def enqueue(self, record: UsageRecord) -> bool:
        self.rows.append(record)
        return True

    async def provider_key(self, workspace_id, provider):  # noqa: ANN001
        return "encrypted::sk-upstream"


class FakeAuth:
    def __init__(self, valid_key: str) -> None:
        self._valid = valid_key

    async def authenticate(self, authorization: str | None):  # noqa: ANN001
        if not authorization:
            return None
        raw = authorization.removeprefix("Bearer ").strip()
        if raw != self._valid:
            return None
        return Principal(
            workspace_id=WORKSPACE_ID, workspace_name="test-ws", key_id=uuid.uuid4()
        )


@pytest.fixture()
def app(tmp_path):
    path = tmp_path / "acpi_prices.json"
    path.write_text(json.dumps(DATASET), encoding="utf-8")
    catalog = AcpiCatalog(path, refresh_seconds=3600)
    catalog.load()

    application = FastAPI()
    application.include_router(proxy.router)
    application.state.acpi = catalog
    application.state.db = FakeDatabase()
    application.state.auth = FakeAuth(API_KEY)
    application.state.http = httpx.AsyncClient(transport=_upstream_transport())
    # The fake db hands back "encrypted::<key>"; strip the marker.
    application.state.decrypt = lambda value: value.split("::", 1)[1]
    return application


def _upstream_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if "api.openai.com" in request.url.host:
            body = json.loads(request.content)
            # Echo the requested model back, the way a real provider does — the
            # gateway prices what was actually served, not what was asked for.
            served = body.get("model", "gpt-4o-mini")
            if body.get("stream"):
                stream = (
                    'data: {"choices":[{"delta":{"content":"hi"}}],"model":"%s"}\n\n'
                    'data: {"choices":[],"usage":{"prompt_tokens":1000,'
                    '"completion_tokens":500}}\n\n'
                    "data: [DONE]\n\n"
                ) % served
                return httpx.Response(
                    200, text=stream, headers={"content-type": "text/event-stream"}
                )
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-1",
                    "object": "chat.completion",
                    "model": served,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "hello"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 1000, "completion_tokens": 500},
                },
            )

        if "api.anthropic.com" in request.url.host:
            return httpx.Response(
                200,
                json={
                    "id": "msg_1",
                    "model": "claude-haiku-4-5",
                    "content": [{"type": "text", "text": "hey"}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 100, "output_tokens": 50},
                },
            )

        return httpx.Response(500, json={"error": "unexpected host"})

    return httpx.MockTransport(handler)


def _post(client: TestClient, path: str, body: dict, key: str = API_KEY, **kwargs):
    return client.post(path, json=body, headers={"authorization": f"Bearer {key}"}, **kwargs)


# --- auth --------------------------------------------------------------------


def test_missing_key_is_401(app):
    with TestClient(app) as client:
        response = client.post("/openai/v1/chat/completions", json={"model": "gpt-4o-mini"})
    assert response.status_code == 401
    assert response.json()["error"]["type"] == "authentication_error"


def test_bad_key_is_401_and_writes_nothing(app):
    with TestClient(app) as client:
        response = _post(
            client, "/openai/v1/chat/completions", {"model": "gpt-4o-mini"}, key="txk-wrong"
        )
    assert response.status_code == 401
    assert app.state.db.rows == []


def test_unknown_provider_is_404(app):
    with TestClient(app) as client:
        response = _post(client, "/cohere/v1/chat/completions", {"model": "x"})
    assert response.status_code == 404


# --- the MVP acceptance test -------------------------------------------------


def test_request_flows_through_and_is_priced(app):
    """One request in, correct response out, one priced ledger row."""
    with TestClient(app) as client:
        response = _post(
            client,
            "/openai/v1/chat/completions",
            {"model": "openai/gpt-4o-mini", "messages": [{"role": "user", "content": "hello"}]},
        )

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "hello"

    assert len(app.state.db.rows) == 1
    row = app.state.db.rows[0]
    assert row.workspace_id == WORKSPACE_ID
    assert row.provider == "openai"
    assert row.model_id == "gpt-4o-mini"
    assert (row.input_tokens, row.output_tokens) == (1000, 500)
    assert row.priced is True
    # 1000 * 0.15/1M + 500 * 0.60/1M
    assert row.cost_usd == pytest.approx(0.00045)
    # 1500 tokens at the $5.00/1M market rate
    assert row.acpi_bench_usd == pytest.approx(0.0075)
    assert row.overpay_usd == pytest.approx(0.00045 - 0.0075)
    assert row.status_code == 200
    assert row.is_stream is False


def test_tags_are_recorded(app):
    with TestClient(app) as client:
        client.post(
            "/openai/v1/chat/completions",
            json={"model": "gpt-4o-mini", "messages": []},
            headers={
                "authorization": f"Bearer {API_KEY}",
                "tokenix-feature": "search",
                "tokenix-workload": "production",
            },
        )
    row = app.state.db.rows[0]
    assert (row.feature_tag, row.workload_tag) == ("search", "production")


def test_anthropic_round_trip_is_openai_shaped(app):
    with TestClient(app) as client:
        response = _post(
            client,
            "/anthropic/v1/chat/completions",
            {
                "model": "claude-haiku-4-5",
                "messages": [
                    {"role": "system", "content": "be terse"},
                    {"role": "user", "content": "hi"},
                ],
            },
        )

    body = response.json()
    assert response.status_code == 200
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == "hey"

    row = app.state.db.rows[0]
    assert row.provider == "anthropic"
    assert (row.input_tokens, row.output_tokens) == (100, 50)
    # 100 * 1.00/1M + 50 * 5.00/1M
    assert row.cost_usd == pytest.approx(0.00035)


def test_streaming_passes_through_and_still_bills(app):
    with TestClient(app) as client:
        with client.stream(
            "POST",
            "/openai/v1/chat/completions",
            json={"model": "gpt-4o-mini", "messages": [], "stream": True},
            headers={"authorization": f"Bearer {API_KEY}"},
        ) as response:
            chunks = list(response.iter_lines())

    assert response.status_code == 200
    assert any("[DONE]" in line for line in chunks)
    assert any('"hi"' in line for line in chunks)

    assert len(app.state.db.rows) == 1
    row = app.state.db.rows[0]
    assert row.is_stream is True
    # Usage arrives in the final chunk; the row must reflect it, not zero.
    assert (row.input_tokens, row.output_tokens) == (1000, 500)
    assert row.cost_usd == pytest.approx(0.00045)


def test_unpriced_model_is_flagged_not_billed_as_free(app):
    with TestClient(app) as client:
        _post(
            client,
            "/openai/v1/chat/completions",
            {"model": "gpt-4o-mini", "messages": []},
        )
    app.state.db.rows.clear()

    # A model absent from the dataset still records tokens, flagged unpriced.
    with TestClient(app) as client:
        _post(
            client,
            "/openai/v1/chat/completions",
            {"model": "some-unlisted-model", "messages": []},
        )
    row = app.state.db.rows[0]
    assert row.priced is False
    assert row.cost_usd == 0.0
