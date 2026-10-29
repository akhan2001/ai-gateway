"""Logs status filter: a 402 is an error, there is no "blocked" status."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi import HTTPException

from src.routes.logs import _build_filters

NOW = datetime.now(timezone.utc)


def _status(status):
    conditions, _ = _build_filters(uuid4(), NOW, NOW, None, status, None, None, None)
    return conditions[3:]


def test_error_includes_402_and_blocked_is_rejected():
    assert _status("error") == ["status_code >= 400"]
    assert _status("success") == ["status_code < 400"]
    assert _status(None) == []
    with pytest.raises(HTTPException) as exc:
        _status("blocked")
    assert exc.value.status_code == 400
