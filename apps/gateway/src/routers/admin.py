"""Workspace + credential provisioning.

This backs the dashboard's `/connect` page. It is *not* customer-facing: it is
guarded by ADMIN_TOKEN and is expected to sit behind the private network, with
the dashboard calling it server-side.
"""

from __future__ import annotations

import os
import secrets
from uuid import UUID

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..middleware.auth import generate_key, hash_key, key_display_prefix
from ..providers.registry import known_providers

router = APIRouter(prefix="/admin")


def _authorized(token: str | None) -> bool:
    expected = os.getenv("ADMIN_TOKEN", "")
    if not expected:
        return False
    # Constant-time compare so the token cannot be recovered by timing.
    return bool(token) and secrets.compare_digest(token, expected)


class CreateWorkspace(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class AddProviderKey(BaseModel):
    provider: str
    api_key: str = Field(min_length=1)


@router.post("/workspaces")
async def create_workspace(
    body: CreateWorkspace,
    request: Request,
    x_admin_token: str | None = Header(default=None),
) -> JSONResponse:
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
    if not _authorized(x_admin_token):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})

    provider = body.provider.strip().lower()
    if provider not in known_providers():
        return JSONResponse(
            status_code=400,
            content={"error": f"unknown provider '{provider}'", "supported": known_providers()},
        )

    encrypted = request.app.state.encrypt(body.api_key)
    await request.app.state.db.pool.execute(
        """
        INSERT INTO provider_keys (workspace_id, provider, encrypted_key)
        VALUES ($1, $2, $3)
        ON CONFLICT (workspace_id, provider)
        DO UPDATE SET encrypted_key = EXCLUDED.encrypted_key
        """,
        workspace_id,
        provider,
        encrypted,
    )
    return JSONResponse(status_code=201, content={"workspace_id": str(workspace_id), "provider": provider})
