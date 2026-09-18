"""Workspace + credential provisioning.

This backs the dashboard's `/connect` page. It is *not* customer-facing: it is
guarded by ADMIN_TOKEN and is expected to sit behind the private network, with
the dashboard calling it server-side.
"""

from __future__ import annotations

import json
import os
import secrets
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..config import settings
from ..middleware.auth import generate_key, hash_key, key_display_prefix
from ..providers.registry import known_providers
from ..services.ratelimit import client_ip, rate_limited_response

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
