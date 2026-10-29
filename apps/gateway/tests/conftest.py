"""Settings are read at import, so test-only environment goes here, first."""

import os

os.environ.setdefault("PREFIX_HASH_SECRET", "test-prefix-hash-secret")
