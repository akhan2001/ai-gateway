"""Workspace auth for the analytics API.

Same `txk-` keys as the gateway, same SHA-256 hashing. The workspace is
resolved **from the key**, never from a query parameter — otherwise any
customer could read another workspace's spend by guessing a UUID.
"""

from __future__ import annotations

import hashlib
from uuid import UUID

from fastapi import Depends, Header, HTTPException

from .db import db

KEY_PREFIX = "txk-"


def hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


async def current_workspace(authorization: str | None = Header(default=None)) -> UUID:
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
