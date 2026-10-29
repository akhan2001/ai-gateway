"""Settings are read at import, so test-only environment goes here, first."""

import os

os.environ.setdefault("PREFIX_HASH_SECRET", "test-prefix-hash-secret")

# The gateway only loads tiktoken encodings from the local cache (the image
# bakes them in). Fill the cache here, as the Dockerfile does, so the
# tokenizer tests run on a fresh checkout.
import tiktoken  # noqa: E402

for _name in ("o200k_base", "cl100k_base"):
    tiktoken.get_encoding(_name)
