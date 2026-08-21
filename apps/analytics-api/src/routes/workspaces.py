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

import hmac
import os
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
               ) AS key_prefix
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
