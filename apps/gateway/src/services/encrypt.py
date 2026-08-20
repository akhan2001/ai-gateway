"""Fernet encryption for stored provider credentials.

Provider keys are decrypted in memory for the duration of one upstream call
and never logged, never returned by an API, and never written back in plain
form.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from ..config import settings

log = logging.getLogger(__name__)


class EncryptionNotConfigured(RuntimeError):
    """Raised when ENCRYPTION_KEY is missing or malformed."""


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    key = settings.encryption_key
    if not key:
        raise EncryptionNotConfigured(
            "ENCRYPTION_KEY is not set. Generate one with:\n"
            "  python -c \"from cryptography.fernet import Fernet; "
            'print(Fernet.generate_key().decode())"'
        )
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except (ValueError, TypeError) as exc:
        raise EncryptionNotConfigured(f"ENCRYPTION_KEY is not a valid Fernet key: {exc}") from exc


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        # Never include the ciphertext or key in the message.
        raise ValueError("stored provider credential could not be decrypted") from exc


def generate_key() -> str:
    """Convenience for `python -m src.services.encrypt`."""
    return Fernet.generate_key().decode()


if __name__ == "__main__":  # pragma: no cover
    print(generate_key())
