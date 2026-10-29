"""Section token counts from the model's own tokenizer, for OpenAI-family requests.

The in-request split (anatomy.split_tokens) apportions the billed input total
by each section's share of serialized characters, which misattributes tokens
between sections that tokenize at different rates (JSON tool schemas vs
prose). Here each section's serialized JSON goes through the model's tiktoken
encoding instead, and the counts are scaled to the same billed total, so the
sections still sum to what the provider billed. The provider's chat template
adds a few tokens per message that this doesn't see; scaling spreads them.

Runs after the response, on a small dedicated thread pool (routers/proxy.py).
Nothing is kept: the encoded tokens exist for one function call.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import tiktoken
import tiktoken.load
from tiktoken.model import encoding_name_for_model
from tiktoken_ext import openai_public

from .anatomy import sections

log = logging.getLogger(__name__)

#: Providers whose models use tiktoken encodings.
TOKENIZED_PROVIDERS = frozenset({"openai", "azure"})

#: Requests that kept the in-request estimate because the queue was full.
#: Exposed on /health; since boot, per process.
dropped: int = 0


def _load_cached(name: str) -> tiktoken.Encoding | None:
    """An encoding from tiktoken's local cache (baked into the image by the
    Dockerfile), never the network: tiktoken fetches on a cache miss, and a
    gateway must not depend on, or wait for, a download. None if not cached.

    Runs once, at import, before any worker thread exists: it swaps
    tiktoken's one network call for an error while it loads.
    """
    real = tiktoken.load.read_file

    def no_network(blobpath: str) -> bytes:
        raise FileNotFoundError(f"not in the tiktoken cache: {blobpath}")

    tiktoken.load.read_file = no_network
    try:
        return tiktoken.Encoding(**openai_public.ENCODING_CONSTRUCTORS[name]())
    except Exception as exc:
        log.error("tiktoken encoding %s not cached (%s); its rows keep the estimate", name, type(exc).__name__)
        return None
    finally:
        tiktoken.load.read_file = real


#: Loaded at startup; a missing one just means its models aren't tokenized.
ENCODINGS: dict[str, tiktoken.Encoding] = {
    name: encoding for name in ("o200k_base", "cl100k_base") if (encoding := _load_cached(name)) is not None
}


def encoding_for(model_id: str) -> tiktoken.Encoding | None:
    try:
        name = encoding_name_for_model(model_id)
    except KeyError:
        # ponytail: tiktoken's name table lags new models (gpt-4.1, gpt-5.x
        # aren't in 0.9); every OpenAI model since gpt-4o uses o200k_base.
        # Pin an explicit table if a family ships on a new encoding.
        name = "o200k_base"
    return ENCODINGS.get(name)


def available(provider: str, model_id: str) -> bool:
    """Whether this row can be tokenized; otherwise it keeps the estimate."""
    return provider in TOKENIZED_PROVIDERS and encoding_for(model_id) is not None


def section_tokens(payload: dict[str, Any], model_id: str, *, forwards_tools: bool) -> tuple[int, int, int, int]:
    """(system, tools, history, latest) tokens of each section's serialized JSON."""
    encoding = encoding_for(model_id)
    if encoding is None:
        raise LookupError(f"no cached encoding for {model_id}")
    counts = [
        len(encoding.encode(json.dumps(part, separators=(",", ":"), ensure_ascii=False), disallowed_special=()))
        if part
        else 0
        for part in sections(payload, forwards_tools=forwards_tools)
    ]
    return counts[0], counts[1], counts[2], counts[3]
