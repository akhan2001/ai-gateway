"""Workspace auth for the analytics API.

Same `txk-` keys as the gateway, same SHA-256 hashing. The workspace is
resolved **from the key**, never from a query parameter — otherwise any
customer could read another workspace's spend by guessing a UUID.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from uuid import UUID

from fastapi import Depends, Header, HTTPException

from .db import db

KEY_PREFIX = "txk-"


def hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


async def current_workspace(
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None),
    x_workspace_id: str | None = Header(default=None),
) -> UUID:
    """Resolve the caller's workspace.

    Two ways in, and the ordering matters:

    * A `txk-` bearer key — the original path, unchanged. Anything already
      calling this API keeps working exactly as before.
    * `x-internal-token` + `x-workspace-id` — server-to-server, used by the
      dashboard once Clerk has authenticated a *human* who owns that workspace.
      Humans sign in with Clerk and never hold a key, so there is no key to
      present here.

    The internal path names its workspace directly, which is only safe because
    the shared token gates it. It is checked with a constant-time compare, an
    empty configured secret is treated as "disabled" rather than "matches
    nothing", and the header is never accepted from a browser — the analytics
    CORS allowlist does not expose it.
    """
    if x_internal_token is not None:
        expected = os.getenv("INTERNAL_API_TOKEN", "")
        if not expected:
            raise HTTPException(status_code=503, detail="Internal API is not configured")
        if not hmac.compare_digest(x_internal_token, expected):
            raise HTTPException(status_code=401, detail="Invalid internal token")
        if not x_workspace_id:
            raise HTTPException(status_code=400, detail="Missing x-workspace-id")
        try:
            workspace_id = UUID(x_workspace_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Malformed x-workspace-id") from None

        row = await db.fetchrow(
            "SELECT id FROM workspaces WHERE id = $1 AND enabled",
            workspace_id,
        )
        if row is None:
            raise HTTPException(status_code=404, detail="No such workspace")
        return row["id"]

    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")

    raw = authorization.strip()
    if raw.lower().startswith("bearer "):
        raw = raw[7:].strip()
    if not raw.startswith(KEY_PREFIX):
        raise HTTPException(status_code=401, detail="Not a Tokenix API key")

    row = await db.fetchrow(
        """
        SELECT k.workspace_id
        FROM api_keys k
        JOIN workspaces w ON w.id = k.workspace_id
        WHERE k.key_hash = $1 AND NOT k.revoked AND w.enabled
        """,
        hash_key(raw),
    )
    if row is None:
        raise HTTPException(status_code=401, detail="Invalid Tokenix API key")
    return row["workspace_id"]


WorkspaceDep = Depends(current_workspace)
