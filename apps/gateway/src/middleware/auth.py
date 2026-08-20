"""`txk-` key authentication.

Keys are hashed with SHA-256, not bcrypt. Bcrypt cannot be used for a lookup
key: each hash carries its own salt, so verifying means trying every stored
hash in turn — a full table scan on the hot path. SHA-256 gives one indexed
lookup. That is safe *because* the key is 32 bytes of CSPRNG output rather
than a user-chosen password: there is no dictionary to attack, so the slow-KDF
property buys nothing here.

Resolved keys are cached (Redis when configured, in-process otherwise) so the
common path costs no database round trip.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from ..config import settings

log = logging.getLogger(__name__)

KEY_PREFIX = "txk-"


def hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def generate_key() -> str:
    return f"{KEY_PREFIX}{secrets.token_urlsafe(32)}"


def key_display_prefix(raw_key: str) -> str:
    return raw_key[:12]


def extract_bearer(header: str | None) -> str | None:
    if not header:
        return None
    value = header.strip()
    if value.lower().startswith("bearer "):
        value = value[7:].strip()
    return value or None


@dataclass(frozen=True)
class Principal:
    """The authenticated caller."""

    workspace_id: UUID
    workspace_name: str
    key_id: UUID


class _LocalCache:
    """Small TTL cache used when Redis is not configured."""

    def __init__(self, ttl_seconds: int, max_entries: int) -> None:
        self._ttl = ttl_seconds
        self._max = max_entries
        self._entries: dict[str, tuple[float, Principal | None]] = {}

    def get(self, key: str) -> tuple[bool, Principal | None]:
        hit = self._entries.get(key)
        if hit is None:
            return False, None
        expires_at, principal = hit
        if expires_at < time.monotonic():
            self._entries.pop(key, None)
            return False, None
        return True, principal

    def put(self, key: str, principal: Principal | None) -> None:
        if len(self._entries) >= self._max:
            # Cheap eviction: drop whatever is oldest by expiry.
            oldest = min(self._entries, key=lambda k: self._entries[k][0])
            self._entries.pop(oldest, None)
        self._entries[key] = (time.monotonic() + self._ttl, principal)

    def clear(self) -> None:
        self._entries.clear()


class Authenticator:
    def __init__(self, db: Any, redis: Any | None = None) -> None:
        self._db = db
        self._redis = redis
        self._local = _LocalCache(
            settings.key_cache_ttl_seconds, settings.key_cache_max_entries
        )

    async def authenticate(self, authorization: str | None) -> Principal | None:
        raw_key = extract_bearer(authorization)
        if not raw_key or not raw_key.startswith(KEY_PREFIX):
            return None

        digest = hash_key(raw_key)
        cache_key = f"tokenix:key:{digest}"

        cached_hit, cached = await self._cache_get(cache_key)
        if cached_hit:
            # Negative results are cached too, so a burst of bad keys cannot
            # turn into a burst of database queries.
            return cached

        row = await self._db.resolve_api_key(digest)
        principal = (
            Principal(
                workspace_id=row["workspace_id"],
                workspace_name=row["workspace_name"],
                key_id=row["key_id"],
            )
            if row
            else None
        )

        await self._cache_put(cache_key, principal)
        return principal

    # -- cache plumbing -----------------------------------------------------

    async def _cache_get(self, cache_key: str) -> tuple[bool, Principal | None]:
        if self._redis is None:
            return self._local.get(cache_key)
        try:
            raw = await self._redis.get(cache_key)
        except Exception:
            log.debug("redis get failed; falling back to database", exc_info=True)
            return False, None
        if raw is None:
            return False, None
        if raw in (b"", "", b"null", "null"):
            return True, None
        try:
            payload = json.loads(raw)
            return True, Principal(
                workspace_id=UUID(payload["workspace_id"]),
                workspace_name=payload["workspace_name"],
                key_id=UUID(payload["key_id"]),
            )
        except (ValueError, KeyError, TypeError):
            return False, None

    async def _cache_put(self, cache_key: str, principal: Principal | None) -> None:
        if self._redis is None:
            self._local.put(cache_key, principal)
            return
        payload = (
            "null"
            if principal is None
            else json.dumps(
                {
                    "workspace_id": str(principal.workspace_id),
                    "workspace_name": principal.workspace_name,
                    "key_id": str(principal.key_id),
                }
            )
        )
        try:
            await self._redis.set(cache_key, payload, ex=settings.key_cache_ttl_seconds)
        except Exception:
            log.debug("redis set failed; caching locally instead", exc_info=True)
            self._local.put(cache_key, principal)
