"""Internal workspace lookup, keyed by Clerk user.

These endpoints are **server-to-server only**. They are authenticated by a
shared `INTERNAL_API_TOKEN`, never by a `txk-` key, because their whole job is
to answer questions for a person who does not have a key yet.

The Clerk user id arrives as a parameter, which is only safe because the token
gates the call: it is supplied by the dashboard's server after it has verified
the Clerk session, and is never accepted from a browser. Trusting an
`x-clerk-user-id` header on a public route would let anyone read any
workspace by naming its owner.

No endpoint here returns a key. `api_keys` stores a SHA-256 hash and a 12-char
prefix; the plaintext is shown once by whoever minted it and is then gone.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from ..db import db

router = APIRouter(prefix="/api/v1/internal")


def _authorize(token: str | None) -> None:
    expected = os.getenv("INTERNAL_API_TOKEN", "")
    # Unset means disabled, not open: an empty secret must never authorize.
    if not expected:
        raise HTTPException(status_code=503, detail="Internal API is not configured")
    if not token or not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="Invalid internal token")


class LinkRequest(BaseModel):
    clerk_user_id: str
    workspace_id: UUID
    email: str | None = None


@router.get("/workspaces/me")
async def get_workspace_for_clerk_user(
    clerk_user_id: str,
    x_internal_token: str | None = Header(default=None),
) -> dict:
    """Resolve the workspace a Clerk user owns, with the key's display prefix."""
    _authorize(x_internal_token)

    row = await db.fetchrow(
        """
        SELECT w.id, w.name, w.email, w.created_at,
               (
                 SELECT k.key_prefix
                 FROM api_keys k
                 WHERE k.workspace_id = w.id AND NOT k.revoked
                 ORDER BY k.created_at DESC
                 LIMIT 1
               ) AS key_prefix,
               EXISTS (
                 SELECT 1 FROM provider_keys pk WHERE pk.workspace_id = w.id
               ) AS has_provider_keys
        FROM workspaces w
        WHERE w.clerk_user_id = $1 AND w.enabled
        """,
        clerk_user_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="No workspace for that user")

    return {
        "workspace_id": str(row["id"]),
        "name": row["name"],
        "email": row["email"],
        "key_prefix": row["key_prefix"],
        "has_provider_keys": row["has_provider_keys"],
        "created_at": row["created_at"].isoformat(),
    }


@router.post("/workspaces/link")
async def link_workspace_to_clerk_user(
    body: LinkRequest,
    x_internal_token: str | None = Header(default=None),
) -> dict:
    """Attach a freshly created workspace to the Clerk user who owns it.

    Idempotent on the Clerk user: if a link already exists the existing
    workspace wins and the caller is told, so a double-submitted first visit
    cannot leave one person owning two workspaces.
    """
    _authorize(x_internal_token)

    existing = await db.fetchrow(
        "SELECT id FROM workspaces WHERE clerk_user_id = $1",
        body.clerk_user_id,
    )
    if existing is not None:
        return {"workspace_id": str(existing["id"]), "created_link": False}

    updated = await db.fetchrow(
        """
        UPDATE workspaces
           SET clerk_user_id = $1, email = $2
        WHERE id = $3 AND clerk_user_id IS NULL
        RETURNING id
        """,
        body.clerk_user_id,
        body.email,
        body.workspace_id,
    )
    if updated is None:
        raise HTTPException(
            status_code=409,
            detail="That workspace does not exist or already belongs to someone else",
        )
    return {"workspace_id": str(updated["id"]), "created_link": True}


class CreateRequest(BaseModel):
    clerk_user_id: str
    email: str | None = None
    name: str | None = None


def _generate_key() -> str:
    """A `txk-` key: 32 bytes of CSPRNG output, URL-safe."""
    return "txk-" + secrets.token_urlsafe(32)


def _hash_key(raw: str) -> str:
    """SHA-256, matching the gateway exactly.

    NOT bcrypt, and this is not a preference. The gateway authenticates every
    proxied request with `WHERE key_hash = $1` — a single indexed lookup.
    Bcrypt puts the salt inside each hash, so verifying one means trying every
    stored hash in turn: a full table scan on the hot path of an inference
    proxy. It is safe to use a fast hash here precisely because the key is 32
    bytes of CSPRNG output rather than a chosen password — there is no
    dictionary to attack.

    The practical consequence matters more than the theory: a key hashed any
    other way would not authenticate at the gateway at all, so the customer
    would be handed a credential that works nowhere.
    """
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@router.post("/workspaces", status_code=201)
async def create_workspace_for_clerk_user(
    body: CreateRequest,
    x_internal_token: str | None = Header(default=None),
) -> dict:
    """Create a workspace and its first key for a Clerk user, in one call.

    Lets the dashboard provision without holding the gateway's admin token,
    which is a meaningfully smaller blast radius for a secret living in Vercel.

    Returns the plaintext key exactly once. Only a SHA-256 hash and a 12-char
    display prefix are stored, so this response is the single opportunity to
    show it to its owner.
    """
    _authorize(x_internal_token)

    existing = await db.fetchrow(
        "SELECT id FROM workspaces WHERE clerk_user_id = $1",
        body.clerk_user_id,
    )
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "That user already has a workspace",
                "workspace_id": str(existing["id"]),
            },
        )

    raw_key = _generate_key()
    name = body.name or (body.email.split("@")[0] if body.email else "workspace")

    assert db.pool is not None, "database pool not started"
    async with db.pool.acquire() as conn:
        # One transaction: a workspace without a key would strand the user with
        # nothing to authenticate with and no way to ask for another.
        async with conn.transaction():
            workspace_id = await conn.fetchval(
                """
                INSERT INTO workspaces (name, clerk_user_id, email)
                VALUES ($1, $2, $3)
                RETURNING id
                """,
                name,
                body.clerk_user_id,
                body.email,
            )
            await conn.execute(
                "INSERT INTO api_keys (workspace_id, key_hash, key_prefix) VALUES ($1, $2, $3)",
                workspace_id,
                _hash_key(raw_key),
                raw_key[:12],
            )

    return {
        "workspace_id": str(workspace_id),
        "name": name,
        "api_key": raw_key,
        "created": True,
        "warning": "Store this key now. It cannot be retrieved again.",
    }


class LinkKeyRequest(BaseModel):
    clerk_user_id: str
    txk_key: str


@router.post("/workspaces/link-key")
async def link_existing_key(
    body: LinkKeyRequest,
    x_internal_token: str | None = Header(default=None),
) -> dict:
    """Claim an existing workspace by presenting its `txk-` key.

    For customers provisioned before Clerk: their workspace has no
    clerk_user_id, so signing in would otherwise strand their history behind a
    fresh empty workspace.

    Possession of the key IS the proof of ownership — it is the same secret
    that authorises spending money through the gateway, so anyone holding it
    already controls the workspace. Brute force is not a concern at 32 bytes of
    CSPRNG entropy, and the lookup is by hash, so an invalid key is
    indistinguishable from a nonexistent one.

    Separate from /workspaces/link, which asserts a workspace id the server
    just created. The two prove ownership in different ways and should not
    share a handler.
    """
    _authorize(x_internal_token)

    raw_key = body.txk_key.strip()
    if not raw_key.startswith("txk-"):
        raise HTTPException(status_code=400, detail="That is not a Tokenix key")

    row = await db.fetchrow(
        """
        SELECT w.id, w.name, w.clerk_user_id, k.key_prefix
        FROM api_keys k
        JOIN workspaces w ON w.id = k.workspace_id
        WHERE k.key_hash = $1 AND NOT k.revoked AND w.enabled
        """,
        _hash_key(raw_key),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="That key was not recognised")

    owner = row["clerk_user_id"]
    if owner is not None and owner != body.clerk_user_id:
        # Someone else's linked workspace. Refuse rather than steal it.
        raise HTTPException(
            status_code=409,
            detail="That workspace is already linked to a different account",
        )

    assert db.pool is not None, "database pool not started"
    async with db.pool.acquire() as conn:
        # One transaction, and the order matters: the partial unique index
        # allows a Clerk user exactly one workspace, so any earlier link — the
        # empty workspace auto-provisioned on first sign-in — has to be
        # released before this one can be claimed.
        async with conn.transaction():
            await conn.execute(
                """
                UPDATE workspaces SET clerk_user_id = NULL
                WHERE clerk_user_id = $1 AND id <> $2
                """,
                body.clerk_user_id,
                row["id"],
            )
            await conn.execute(
                "UPDATE workspaces SET clerk_user_id = $1 WHERE id = $2",
                body.clerk_user_id,
                row["id"],
            )

    return {
        "workspace_id": str(row["id"]),
        "name": row["name"],
        "key_prefix": row["key_prefix"],
        "linked": True,
    }
