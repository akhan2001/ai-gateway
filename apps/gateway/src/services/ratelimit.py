"""Token-bucket rate limiting backed by Upstash Redis's REST API.

Deliberately separate from the `redis.asyncio` connection in
`middleware/auth.py`: that one talks REDIS_URL over the wire protocol, this
one talks Upstash's HTTPS REST API (UPSTASH_REDIS_REST_URL /
UPSTASH_REDIS_REST_TOKEN). Keeping them independent means an outage in either
Redis-shaped dependency doesn't also take down the other.

Fails open: if Upstash is unreachable or errors, requests are allowed through
rather than a rate-limiter hiccup taking the gateway down.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse

log = logging.getLogger(__name__)

# Atomic refill-then-consume of a token bucket, run server-side via Upstash's
# EVAL so concurrent requests against the same key can't race each other the
# way a read-then-write from Python would.
#
# KEYS[1]  bucket key
# ARGV[1]  capacity (= the per-minute limit)
# ARGV[2]  refill rate, tokens/second
# ARGV[3]  now, epoch ms
_TOKEN_BUCKET_SCRIPT = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_rate = tonumber(ARGV[2])
local now = tonumber(ARGV[3])

local bucket = redis.call("HMGET", key, "tokens", "ts")
local tokens = tonumber(bucket[1])
local last = tonumber(bucket[2])
if tokens == nil then
  tokens = capacity
  last = now
end

local elapsed = math.max(0, now - last) / 1000
tokens = math.min(capacity, tokens + elapsed * refill_rate)

local allowed = 0
if tokens >= 1 then
  tokens = tokens - 1
  allowed = 1
end

redis.call("HMSET", key, "tokens", tostring(tokens), "ts", tostring(now))
redis.call("PEXPIRE", key, math.ceil((capacity / refill_rate) * 1000) + 1000)

return {allowed, tostring(tokens)}
"""


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    retry_after_seconds: int


class RateLimiter:
    def __init__(self, rest_url: str, rest_token: str) -> None:
        self._client = httpx.AsyncClient(
            base_url=rest_url.rstrip("/"),
            headers={"Authorization": f"Bearer {rest_token}"},
            timeout=httpx.Timeout(2.0, connect=1.0),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def check(self, scope: str, identifier: str, limit_per_minute: int) -> RateLimitResult:
        """Consume one token from the `scope:identifier` bucket.

        The bucket holds `limit_per_minute` tokens and refills continuously
        at `limit_per_minute / 60` tokens/second, so a burst up to the limit
        is allowed but the sustained rate is capped at the limit per minute.
        """
        key = f"ratelimit:{scope}:{identifier}"
        refill_rate = limit_per_minute / 60.0
        now_ms = int(time.time() * 1000)

        try:
            resp = await self._client.post(
                "/",
                json=[
                    "EVAL",
                    _TOKEN_BUCKET_SCRIPT,
                    "1",
                    key,
                    str(limit_per_minute),
                    str(refill_rate),
                    str(now_ms),
                ],
            )
            resp.raise_for_status()
            result = resp.json()["result"]
            allowed = bool(int(result[0]))
            tokens_left = float(result[1])
        except Exception:
            log.warning("rate limiter unreachable; allowing request through", exc_info=True)
            return RateLimitResult(allowed=True, retry_after_seconds=0)

        if allowed:
            return RateLimitResult(allowed=True, retry_after_seconds=0)

        missing = max(0.0, 1.0 - tokens_left)
        retry_after = max(1, math.ceil(missing / refill_rate))
        return RateLimitResult(allowed=False, retry_after_seconds=retry_after)


def client_ip(request: Request) -> str:
    """Best-effort caller IP, preferring the proxy-set header Railway adds
    in front of the app over the socket peer (which would otherwise be
    Railway's own edge)."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def rate_limited_response(retry_after_seconds: int) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={
            "error": {
                "message": "Rate limit exceeded. Try again later.",
                "type": "rate_limit_error",
            }
        },
        headers={"Retry-After": str(retry_after_seconds)},
    )
