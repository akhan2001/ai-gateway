"""GET /api/v1/cost-methods: whether a page's range spans the net-of-cache switch."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from fastapi.testclient import TestClient

from src import db as dbmod
from src.auth import current_workspace
from src.main import app

WORKSPACE = uuid4()
SWITCH = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)


def _get(monkeypatch, row, since="2026-10-01T00:00:00Z"):  # noqa: ANN001
    seen = []

    async def fake_fetchrow(query: str, *args):
        seen.append(args)
        return row

    monkeypatch.setattr(dbmod.db, "fetchrow", fake_fetchrow)
    app.dependency_overrides[current_workspace] = lambda: WORKSPACE
    try:
        response = TestClient(app).get("/api/v1/cost-methods", params={"since": since} if since else {})
    finally:
        app.dependency_overrides.clear()
    return response, seen


def test_range_crossing_the_switch(monkeypatch):
    response, seen = _get(monkeypatch, {"switched_at": SWITCH, "crosses": True})
    assert response.json() == {"switched_at": SWITCH.isoformat(), "crosses": True}
    workspace, since = seen[0]
    assert workspace == WORKSPACE and since == datetime(2026, 10, 1, tzinfo=timezone.utc)


def test_no_switch_yet(monkeypatch):
    response, _ = _get(monkeypatch, {"switched_at": None, "crosses": False})
    assert response.json() == {"switched_at": None, "crosses": False}


def test_since_is_required(monkeypatch):
    response, seen = _get(monkeypatch, None, since=None)
    assert response.status_code == 422 and seen == []
