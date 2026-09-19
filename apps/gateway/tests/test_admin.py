"""Provider-key admin endpoints: list, delete, and test-before-save."""

from __future__ import annotations

import datetime
import uuid

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.routers import admin

ADMIN_TOKEN = "test-admin-token"
WORKSPACE_ID = uuid.uuid4()


class FakePool:
    """In-memory stand-in for asyncpg's pool, just the calls admin.py makes."""

    def __init__(self) -> None:
        self.rows: list[dict] = []

    async def execute(self, query: str, *args):  # noqa: ANN001
        query = query.strip()
        if query.startswith("INSERT"):
            workspace_id, provider, encrypted_key, config = args
            self.rows = [r for r in self.rows if r["provider"] != provider]
            self.rows.append(
                {"provider": provider, "encrypted_key": encrypted_key, "config": config, "created_at": _NOW}
            )
            return "INSERT 0 1"
        if query.startswith("DELETE"):
            workspace_id, provider = args
            before = len(self.rows)
            self.rows = [r for r in self.rows if r["provider"] != provider]
            return "DELETE 1" if len(self.rows) < before else "DELETE 0"
        raise AssertionError(f"unexpected query: {query}")

    async def fetch(self, query: str, *args):  # noqa: ANN001
        assert query.strip().startswith("SELECT")
        return [dict(r) for r in self.rows]


_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)


class FakeDB:
    def __init__(self) -> None:
        self.pool = FakePool()


@pytest.fixture()
def app(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", ADMIN_TOKEN)

    application = FastAPI()
    application.include_router(admin.router)
    application.state.db = FakeDB()
    application.state.ratelimit = None
    application.state.encrypt = lambda raw: f"encrypted::{raw}"
    application.state.http = httpx.AsyncClient(transport=httpx.MockTransport(_upstream_handler))
    return application


def _upstream_handler(request: httpx.Request) -> httpx.Response:
    if request.headers.get("authorization") == "Bearer fail-auth":
        return httpx.Response(401, json={"error": {"message": "Invalid API key provided"}})
    return httpx.Response(200, json={"id": "x", "choices": [{"message": {"content": "hi"}}]})


def _headers(token: str | None = ADMIN_TOKEN) -> dict:
    return {"x-admin-token": token} if token else {}


def test_endpoints_reject_missing_or_wrong_token(app):
    with TestClient(app) as client:
        assert client.get(f"/admin/workspaces/{WORKSPACE_ID}/provider-keys").status_code == 401
        assert client.get(
            f"/admin/workspaces/{WORKSPACE_ID}/provider-keys", headers=_headers("wrong")
        ).status_code == 401
        assert (
            client.delete(f"/admin/workspaces/{WORKSPACE_ID}/provider-keys/openai").status_code
            == 401
        )
        assert (
            client.post("/admin/provider-keys/test", json={"provider": "openai", "api_key": "sk-x"}).status_code
            == 401
        )


def test_save_then_list_then_delete_round_trip(app):
    with TestClient(app) as client:
        created = client.post(
            f"/admin/workspaces/{WORKSPACE_ID}/provider-keys",
            json={"provider": "openai", "api_key": "sk-test"},
            headers=_headers(),
        )
        assert created.status_code == 201

        listed = client.get(f"/admin/workspaces/{WORKSPACE_ID}/provider-keys", headers=_headers())
        assert listed.status_code == 200
        [row] = listed.json()
        assert row["provider"] == "openai"
        # The encrypted secret itself must never come back from this endpoint.
        assert "encrypted_key" not in row and "api_key" not in row

        deleted = client.delete(
            f"/admin/workspaces/{WORKSPACE_ID}/provider-keys/openai", headers=_headers()
        )
        assert deleted.status_code == 200

        missing = client.delete(
            f"/admin/workspaces/{WORKSPACE_ID}/provider-keys/openai", headers=_headers()
        )
        assert missing.status_code == 404


def test_test_provider_key_success(app):
    with TestClient(app) as client:
        response = client.post(
            "/admin/provider-keys/test",
            json={"provider": "openai", "api_key": "sk-good"},
            headers=_headers(),
        )
    assert response.status_code == 200
    assert response.json() == {"success": True}


def test_test_provider_key_surfaces_provider_error(app):
    with TestClient(app) as client:
        response = client.post(
            "/admin/provider-keys/test",
            json={"provider": "openai", "api_key": "fail-auth"},
            headers=_headers(),
        )
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is False
    assert "Invalid API key" in body["error"]


def test_test_provider_key_azure_without_resource_name_fails_cleanly(app):
    with TestClient(app) as client:
        response = client.post(
            "/admin/provider-keys/test",
            json={"provider": "azure", "api_key": "sk-azure"},
            headers=_headers(),
        )
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is False
    assert "resource" in body["error"].lower()


def test_test_provider_key_unknown_provider(app):
    with TestClient(app) as client:
        response = client.post(
            "/admin/provider-keys/test",
            json={"provider": "not-a-provider", "api_key": "sk-x"},
            headers=_headers(),
        )
    assert response.status_code == 400
