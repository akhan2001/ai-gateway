"""txk- API key admin endpoints: mint an additional key, list, revoke."""

from __future__ import annotations

import datetime
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.middleware.auth import hash_key
from src.routers import admin

ADMIN_TOKEN = "test-admin-token"
WORKSPACE_ID = uuid.uuid4()
_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)


class FakePool:
    def __init__(self) -> None:
        self.keys: list[dict] = []
        self._next_id = 1

    async def fetchval(self, query: str, *args):  # noqa: ANN001
        query = query.strip()
        if query.startswith("SELECT 1 FROM workspaces"):
            (workspace_id,) = args
            return 1 if workspace_id == WORKSPACE_ID else None
        if query.startswith("INSERT INTO api_keys"):
            workspace_id, key_hash, key_prefix = args
            key_id = uuid.uuid4()
            self.keys.append(
                {
                    "id": key_id,
                    "workspace_id": workspace_id,
                    "key_hash": key_hash,
                    "key_prefix": key_prefix,
                    "created_at": _NOW,
                    "last_used_at": None,
                    "revoked": False,
                }
            )
            return key_id
        raise AssertionError(f"unexpected fetchval query: {query}")

    async def fetch(self, query: str, *args):  # noqa: ANN001
        assert query.strip().startswith("SELECT id, key_prefix")
        (workspace_id,) = args
        return [
            dict(k) for k in self.keys if k["workspace_id"] == workspace_id and not k["revoked"]
        ]

    async def fetchrow(self, query: str, *args):  # noqa: ANN001
        assert query.strip().startswith("UPDATE api_keys")
        key_id, workspace_id = args
        for k in self.keys:
            if k["id"] == key_id and k["workspace_id"] == workspace_id and not k["revoked"]:
                k["revoked"] = True
                return {"key_hash": k["key_hash"]}
        return None


class FakeDB:
    def __init__(self) -> None:
        self.pool = FakePool()


class FakeRedis:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def delete(self, key: str) -> None:
        self.deleted.append(key)


@pytest.fixture()
def app(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", ADMIN_TOKEN)

    application = FastAPI()
    application.include_router(admin.router)
    application.state.db = FakeDB()
    application.state.ratelimit = None
    application.state.redis = FakeRedis()
    return application


def _headers() -> dict:
    return {"x-admin-token": ADMIN_TOKEN}


def test_create_requires_existing_workspace(app):
    with TestClient(app) as client:
        response = client.post(
            f"/admin/workspaces/{uuid.uuid4()}/keys", json={}, headers=_headers()
        )
    assert response.status_code == 404


def test_create_returns_raw_key_once(app):
    with TestClient(app) as client:
        response = client.post(
            f"/admin/workspaces/{WORKSPACE_ID}/keys", json={}, headers=_headers()
        )
    assert response.status_code == 201
    body = response.json()
    assert body["api_key"].startswith("txk-")
    assert body["key_prefix"] == body["api_key"][:12]
    # It's stored hashed, never in the clear.
    assert app.state.db.pool.keys[-1]["key_hash"] == hash_key(body["api_key"])


def test_list_then_revoke_round_trip(app):
    with TestClient(app) as client:
        created = client.post(
            f"/admin/workspaces/{WORKSPACE_ID}/keys", json={}, headers=_headers()
        ).json()

        listed = client.get(f"/admin/workspaces/{WORKSPACE_ID}/keys", headers=_headers())
        assert listed.status_code == 200
        [row] = listed.json()
        assert row["id"] == created["id"]
        assert row["key_prefix"] == created["key_prefix"]
        # The hash and raw key must never come back from the list endpoint.
        assert "key_hash" not in row and "api_key" not in row

        revoked = client.post(
            f"/admin/workspaces/{WORKSPACE_ID}/keys/{created['id']}/revoke", headers=_headers()
        )
        assert revoked.status_code == 200

        # A revoked key drops out of the list...
        listed_after = client.get(f"/admin/workspaces/{WORKSPACE_ID}/keys", headers=_headers())
        assert listed_after.json() == []

        # ...and revoking it again is a 404, not a silent no-op.
        revoked_again = client.post(
            f"/admin/workspaces/{WORKSPACE_ID}/keys/{created['id']}/revoke", headers=_headers()
        )
        assert revoked_again.status_code == 404


def test_revoke_invalidates_the_auth_cache(app):
    with TestClient(app) as client:
        created = client.post(
            f"/admin/workspaces/{WORKSPACE_ID}/keys", json={}, headers=_headers()
        ).json()
        client.post(f"/admin/workspaces/{WORKSPACE_ID}/keys/{created['id']}/revoke", headers=_headers())

    expected_cache_key = f"tokenix:key:{hash_key(created['api_key'])}"
    assert expected_cache_key in app.state.redis.deleted


def test_endpoints_reject_missing_token(app):
    with TestClient(app) as client:
        assert client.post(f"/admin/workspaces/{WORKSPACE_ID}/keys", json={}).status_code == 401
        assert client.get(f"/admin/workspaces/{WORKSPACE_ID}/keys").status_code == 401
        assert (
            client.post(f"/admin/workspaces/{WORKSPACE_ID}/keys/{uuid.uuid4()}/revoke").status_code
            == 401
        )
