"""scripts/rotate_encryption_key.py: every row ends up under the new key."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from cryptography.fernet import Fernet

_spec = importlib.util.spec_from_file_location("rotate", Path(__file__).parents[1] / "scripts" / "rotate_encryption_key.py")
rotate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rotate)


def test_rotates_old_rows_accepts_new_ones_and_reports_strays():
    old, new, stray = (Fernet.generate_key().decode() for _ in range(3))
    rows = [
        (1, Fernet(old).encrypt(b"sk-one").decode()),
        (2, Fernet(new).encrypt(b"sk-two").decode()),  # saved mid-rotation
        (3, Fernet(stray).encrypt(b"sk-three").decode()),
    ]
    rotated, failed = rotate.rotate_all(rows, old, new)
    assert failed == [3]
    assert {row_id: Fernet(new).decrypt(token.encode()) for row_id, token in rotated} == {1: b"sk-one", 2: b"sk-two"}
