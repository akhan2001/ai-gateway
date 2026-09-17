"""RateLimiter tests against a mocked Upstash REST transport — no network."""

from __future__ import annotations

import json

import httpx
import pytest

from src.services.ratelimit import RateLimiter


def _limiter(handler) -> RateLimiter:
    limiter = RateLimiter("https://example.upstash.io", "test-token")
    limiter._client = httpx.AsyncClient(
        base_url="https://example.upstash.io", transport=httpx.MockTransport(handler)
    )
    return limiter


@pytest.mark.asyncio
async def test_allows_when_bucket_has_tokens():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result": [1, "4.0"]})

    limiter = _limiter(handler)
    result = await limiter.check("workspace", "ws-1", 5)

    assert result.allowed is True
    assert result.retry_after_seconds == 0


@pytest.mark.asyncio
async def test_blocks_when_bucket_is_empty_and_sets_retry_after():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result": [0, "0.0"]})

    limiter = _limiter(handler)
    result = await limiter.check("workspace", "ws-1", 60)

    assert result.allowed is False
    # 60/min => 1 token/sec refill; missing exactly 1 token => ~1s.
    assert result.retry_after_seconds == 1


@pytest.mark.asyncio
async def test_sends_eval_with_expected_key_and_capacity():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen["command"] = body
        return httpx.Response(200, json={"result": [1, "9.0"]})

    limiter = _limiter(handler)
    await limiter.check("admin", "1.2.3.4", 5)

    command = seen["command"]
    assert command[0] == "EVAL"
    assert command[2] == "1"
    assert command[3] == "ratelimit:admin:1.2.3.4"
    assert command[4] == "5"


@pytest.mark.asyncio
async def test_fails_open_when_upstash_is_unreachable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    limiter = _limiter(handler)
    result = await limiter.check("workspace", "ws-1", 60)

    assert result.allowed is True


@pytest.mark.asyncio
async def test_fails_open_on_malformed_upstash_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "shape"})

    limiter = _limiter(handler)
    result = await limiter.check("workspace", "ws-1", 60)

    assert result.allowed is True
