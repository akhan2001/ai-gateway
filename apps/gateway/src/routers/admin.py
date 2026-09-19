"""Workspace + credential provisioning.

This backs the dashboard's `/connect` page. It is *not* customer-facing: it is
guarded by ADMIN_TOKEN and is expected to sit behind the private network, with
the dashboard calling it server-side.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from typing import Any
from uuid import UUID

import httpx
from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..config import settings
from ..middleware.auth import generate_key, hash_key, key_display_prefix
from ..providers.azure import AzureConfigError
from ..providers.registry import get_adapter, known_providers
from ..services.ratelimit import client_ip, rate_limited_response

log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin")


def _authorized(token: str | None) -> bool:
    expected = os.getenv("ADMIN_TOKEN", "")
    if not expected:
        return False
    # Constant-time compare so the token cannot be recovered by timing.
    return bool(token) and secrets.compare_digest(token, expected)


async def _rate_limited(request: Request) -> JSONResponse | None:
    """Checked before `_authorized` too: this endpoint sits in front of a
    database write, and a brute-forcer doesn't need a valid token to be
    worth throttling."""
    limiter = request.app.state.ratelimit
    if limiter is None:
        return None
    result = await limiter.check(
        "admin", client_ip(request), settings.rate_limit_admin_ip_per_minute
    )
    return None if result.allowed else rate_limited_response(result.retry_after_seconds)


class CreateWorkspace(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class AddProviderKey(BaseModel):
    provider: str
    api_key: str = Field(min_length=1)
    # Provider-specific connection metadata beyond the secret itself. Azure
    # requires at least {"resource_name": "..."}; optionally
    # {"api_version": "...", "deployments": {"<model>": "<deployment>"}}.
    # Ignored by providers that only need an API key.
    config: dict[str, Any] | None = None


@router.post("/workspaces")
async def create_workspace(
    body: CreateWorkspace,
    request: Request,
    x_admin_token: str | None = Header(default=None),
) -> JSONResponse:
    if (limited := await _rate_limited(request)) is not None:
        return limited
    if not _authorized(x_admin_token):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})

    raw_key = generate_key()
    pool = request.app.state.db.pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            workspace_id = await conn.fetchval(
                "INSERT INTO workspaces (name) VALUES ($1) RETURNING id", body.name
            )
            await conn.execute(
                """
                INSERT INTO api_keys (workspace_id, key_hash, key_prefix)
                VALUES ($1, $2, $3)
                """,
                workspace_id,
                hash_key(raw_key),
                key_display_prefix(raw_key),
            )

    # The raw key is returned exactly once and never stored.
    return JSONResponse(
        status_code=201,
        content={
            "workspace_id": str(workspace_id),
            "name": body.name,
            "api_key": raw_key,
            "warning": "Store this key now. It cannot be retrieved again.",
        },
    )


@router.post("/workspaces/{workspace_id}/keys")
async def create_api_key(
    workspace_id: UUID,
    request: Request,
    x_admin_token: str | None = Header(default=None),
) -> JSONResponse:
    """Mint an additional `txk-` key on an existing workspace.

    Distinct from `POST /workspaces`, which mints a workspace's first key as
    part of creating the workspace itself — this is for a workspace that
    already exists and wants a second (or a replacement after losing one).
    """
    if (limited := await _rate_limited(request)) is not None:
        return limited
    if not _authorized(x_admin_token):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})

    exists = await request.app.state.db.pool.fetchval(
        "SELECT 1 FROM workspaces WHERE id = $1", workspace_id
    )
    if not exists:
        return JSONResponse(status_code=404, content={"error": "unknown workspace"})

    raw_key = generate_key()
    key_id = await request.app.state.db.pool.fetchval(
        """
        INSERT INTO api_keys (workspace_id, key_hash, key_prefix)
        VALUES ($1, $2, $3)
        RETURNING id
        """,
        workspace_id,
        hash_key(raw_key),
        key_display_prefix(raw_key),
    )

    # The raw key is returned exactly once and never stored.
    return JSONResponse(
        status_code=201,
        content={
            "id": str(key_id),
            "api_key": raw_key,
            "key_prefix": key_display_prefix(raw_key),
            "warning": "Store this key now. It cannot be retrieved again.",
        },
    )


@router.get("/workspaces/{workspace_id}/keys")
async def list_api_keys(
    workspace_id: UUID,
    request: Request,
    x_admin_token: str | None = Header(default=None),
) -> JSONResponse:
    if (limited := await _rate_limited(request)) is not None:
        return limited
    if not _authorized(x_admin_token):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})

    rows = await request.app.state.db.pool.fetch(
        """
        SELECT id, key_prefix, created_at, last_used_at
        FROM api_keys
        WHERE workspace_id = $1 AND NOT revoked
        ORDER BY created_at
        """,
        workspace_id,
    )
    return JSONResponse(
        status_code=200,
        content=[
            {
                "id": str(row["id"]),
                "key_prefix": row["key_prefix"],
                "created_at": row["created_at"].isoformat(),
                "last_used_at": row["last_used_at"].isoformat() if row["last_used_at"] else None,
            }
            for row in rows
        ],
    )


@router.post("/workspaces/{workspace_id}/keys/{key_id}/revoke")
async def revoke_api_key(
    workspace_id: UUID,
    key_id: UUID,
    request: Request,
    x_admin_token: str | None = Header(default=None),
) -> JSONResponse:
    if (limited := await _rate_limited(request)) is not None:
        return limited
    if not _authorized(x_admin_token):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})

    row = await request.app.state.db.pool.fetchrow(
        """
        UPDATE api_keys SET revoked = TRUE
        WHERE id = $1 AND workspace_id = $2 AND NOT revoked
        RETURNING key_hash
        """,
        key_id,
        workspace_id,
    )
    if row is None:
        return JSONResponse(status_code=404, content={"error": "no active key with that id"})

    # Auth is cache-first (see middleware/auth.py) — without this, a revoked
    # key keeps authenticating for up to KEY_CACHE_TTL_SECONDS. Best-effort:
    # only reaches the shared Redis cache, not another gateway instance's
    # in-process fallback cache.
    # ponytail: no cross-instance invalidation when Redis isn't configured;
    # revisit if the gateway ever runs multi-instance without Redis.
    redis = request.app.state.redis
    if redis is not None:
        try:
            await redis.delete(f"tokenix:key:{row['key_hash']}")
        except Exception:
            log.debug("redis cache invalidation failed after key revoke", exc_info=True)

    return JSONResponse(status_code=200, content={"success": True})


@router.post("/workspaces/{workspace_id}/provider-keys")
async def add_provider_key(
    workspace_id: UUID,
    body: AddProviderKey,
    request: Request,
    x_admin_token: str | None = Header(default=None),
) -> JSONResponse:
    if (limited := await _rate_limited(request)) is not None:
        return limited
    if not _authorized(x_admin_token):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})

    provider = body.provider.strip().lower()
    if provider not in known_providers():
        return JSONResponse(
            status_code=400,
            content={"error": f"unknown provider '{provider}'", "supported": known_providers()},
        )

    if provider == "azure" and not (body.config or {}).get("resource_name"):
        return JSONResponse(
            status_code=400,
            content={
                "error": "azure requires config.resource_name (e.g. 'my-company-openai', "
                "the name in https://<resource>.openai.azure.com)"
            },
        )

    encrypted = request.app.state.encrypt(body.api_key)
    config_json = json.dumps(body.config) if body.config is not None else None
    await request.app.state.db.pool.execute(
        """
        INSERT INTO provider_keys (workspace_id, provider, encrypted_key, config)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (workspace_id, provider)
        DO UPDATE SET encrypted_key = EXCLUDED.encrypted_key, config = EXCLUDED.config
        """,
        workspace_id,
        provider,
        encrypted,
        config_json,
    )
    return JSONResponse(status_code=201, content={"workspace_id": str(workspace_id), "provider": provider})


@router.get("/workspaces/{workspace_id}/provider-keys")
async def list_provider_keys(
    workspace_id: UUID,
    request: Request,
    x_admin_token: str | None = Header(default=None),
) -> JSONResponse:
    if (limited := await _rate_limited(request)) is not None:
        return limited
    if not _authorized(x_admin_token):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})

    rows = await request.app.state.db.pool.fetch(
        "SELECT provider, created_at, config FROM provider_keys WHERE workspace_id = $1 ORDER BY created_at",
        workspace_id,
    )
    return JSONResponse(
        status_code=200,
        content=[
            {
                "provider": row["provider"],
                "created_at": row["created_at"].isoformat(),
                "config": json.loads(row["config"]) if isinstance(row["config"], str) else row["config"],
            }
            for row in rows
        ],
    )


@router.delete("/workspaces/{workspace_id}/provider-keys/{provider}")
async def delete_provider_key(
    workspace_id: UUID,
    provider: str,
    request: Request,
    x_admin_token: str | None = Header(default=None),
) -> JSONResponse:
    if (limited := await _rate_limited(request)) is not None:
        return limited
    if not _authorized(x_admin_token):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})

    result = await request.app.state.db.pool.execute(
        "DELETE FROM provider_keys WHERE workspace_id = $1 AND provider = $2",
        workspace_id,
        provider.strip().lower(),
    )
    if result == "DELETE 0":
        return JSONResponse(status_code=404, content={"error": "no credential stored for that provider"})
    return JSONResponse(status_code=200, content={"success": True})


class TestProviderKey(BaseModel):
    provider: str
    api_key: str = Field(min_length=1)
    config: dict[str, Any] | None = None


# Smallest real, paid request that proves a credential authenticates —
# max_tokens=1 keeps the cost negligible. Not customer-configurable: this is
# purely a connectivity check, never a model the customer would actually use.
_TEST_MODELS = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-3-5-haiku-20241022",
    "google": "gemini-2.0-flash",
    "azure": "gpt-4o-mini",
}


def _provider_error_detail(status_code: int, body: bytes) -> str:
    try:
        parsed = json.loads(body)
    except ValueError:
        return body.decode("utf-8", errors="replace")[:300] or f"HTTP {status_code}"
    error = parsed.get("error") if isinstance(parsed, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or error)
    if isinstance(error, str):
        return error
    return str(parsed)[:300]


@router.post("/provider-keys/test")
async def test_provider_key(
    body: TestProviderKey,
    request: Request,
    x_admin_token: str | None = Header(default=None),
) -> JSONResponse:
    """Validate a credential against the real provider before it's saved.

    Stateless and workspace-independent — nothing is persisted here, so this
    doesn't need a workspace_id, just the admin token every other endpoint on
    this router requires.
    """
    if (limited := await _rate_limited(request)) is not None:
        return limited
    if not _authorized(x_admin_token):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})

    provider = body.provider.strip().lower()
    adapter = get_adapter(provider)
    if adapter is None:
        return JSONResponse(
            status_code=400,
            content={"error": f"unknown provider '{provider}'", "supported": known_providers()},
        )

    payload = {
        "model": _TEST_MODELS[provider],
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1,
    }
    try:
        translated = adapter.build_request("v1/chat/completions", payload, body.api_key, body.config)
    except AzureConfigError as exc:
        return JSONResponse(status_code=200, content={"success": False, "error": str(exc)})

    try:
        response = await request.app.state.http.post(
            translated.url, json=translated.payload, headers=translated.headers
        )
    except httpx.HTTPError:
        return JSONResponse(status_code=200, content={"success": False, "error": "Could not reach the provider."})

    if response.status_code < 400:
        return JSONResponse(status_code=200, content={"success": True})
    return JSONResponse(
        status_code=200,
        content={"success": False, "error": _provider_error_detail(response.status_code, response.content)},
    )
