"""One-time re-encryption of every stored provider key under a new ENCRYPTION_KEY.

    OLD_ENCRYPTION_KEY=... NEW_ENCRYPTION_KEY=... DATABASE_URL=... \\
        python scripts/rotate_encryption_key.py [--dry-run]

All rows are rewritten in one transaction, or none are: if any row decrypts
with neither key, nothing is written. Rows already under the new key are
accepted, so it is safe to run again (e.g. for a key saved mid-rotation).

Prints counts and row ids only. Never prints keys, ciphertext, or plaintext.
"""

from __future__ import annotations

import asyncio
import os
import sys

import asyncpg
from cryptography.fernet import Fernet, InvalidToken, MultiFernet


def rotate_all(rows: list[tuple[object, str]], old_key: str, new_key: str) -> tuple[list[tuple[object, str]], list[object]]:
    """(re-encrypted rows, ids that decrypt with neither key). The new key is
    primary, so every token comes back encrypted under it."""
    keys = MultiFernet([Fernet(new_key.encode()), Fernet(old_key.encode())])
    rotated, failed = [], []
    for row_id, token in rows:
        try:
            rotated.append((row_id, keys.rotate(token.encode()).decode()))
        except InvalidToken:
            failed.append(row_id)
    return rotated, failed


def _dsn(raw: str) -> str:
    scheme, _, rest = raw.partition("://")
    return f"{scheme.split('+', 1)[0]}://{rest}"


async def main(dry_run: bool) -> int:
    old_key, new_key, url = (os.environ.get(name, "").strip() for name in ("OLD_ENCRYPTION_KEY", "NEW_ENCRYPTION_KEY", "DATABASE_URL"))
    if not (old_key and new_key and url):
        print("Set OLD_ENCRYPTION_KEY, NEW_ENCRYPTION_KEY and DATABASE_URL.")
        return 2
    if old_key == new_key:
        print("The new key is the same as the old one.")
        return 2
    try:
        Fernet(old_key.encode()), Fernet(new_key.encode())
    except ValueError:
        print("A key is not a valid Fernet key (32 url-safe base64 bytes).")
        return 2

    conn = await asyncpg.connect(_dsn(url))
    try:
        async with conn.transaction():
            # Lock the rows so a key saved during the run can't be skipped.
            rows = await conn.fetch("SELECT id, encrypted_key FROM provider_keys ORDER BY id FOR UPDATE")
            rotated, failed = rotate_all([(r["id"], r["encrypted_key"]) for r in rows], old_key, new_key)
            if failed:
                print(f"{len(failed)} of {len(rows)} rows decrypt with neither key; nothing written:")
                for row_id in failed:
                    print(f"  provider_keys.id = {row_id}")
                return 1
            if dry_run:
                print(f"Dry run: all {len(rows)} rows decrypt and would be re-encrypted. Nothing written.")
                return 0
            await conn.executemany("UPDATE provider_keys SET encrypted_key = $2 WHERE id = $1", rotated)
            # Verify inside the transaction, with the new key alone, before committing.
            check = Fernet(new_key.encode())
            for row in await conn.fetch("SELECT encrypted_key FROM provider_keys"):
                check.decrypt(row["encrypted_key"].encode())  # InvalidToken here rolls back
        print(f"Re-encrypted {len(rotated)} rows under the new key. Committed.")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main("--dry-run" in sys.argv[1:])))
