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
from functools import lru_cache
from typing import Any

import tiktoken

from .anatomy import sections

#: Providers whose models use tiktoken encodings.
TOKENIZED_PROVIDERS = frozenset({"openai", "azure"})

#: Requests that kept the in-request estimate because the queue was full.
#: Exposed on /health; since boot, per process.
dropped: int = 0


@lru_cache(maxsize=64)
def encoding_for(model_id: str) -> tiktoken.Encoding:
    try:
        return tiktoken.encoding_for_model(model_id)
    except KeyError:
        # ponytail: tiktoken's name table lags new models (gpt-4.1, gpt-5.x
        # aren't in 0.9); every OpenAI model since gpt-4o uses o200k_base.
        # Pin an explicit table if a family ships on a new encoding.
        return tiktoken.get_encoding("o200k_base")


def section_tokens(payload: dict[str, Any], model_id: str, *, forwards_tools: bool) -> tuple[int, int, int, int]:
    """(system, tools, history, latest) tokens of each section's serialized JSON."""
    encoding = encoding_for(model_id)
    counts = [
        len(encoding.encode(json.dumps(part, separators=(",", ":"), ensure_ascii=False), disallowed_special=()))
        if part
        else 0
        for part in sections(payload, forwards_tools=forwards_tools)
    ]
    return counts[0], counts[1], counts[2], counts[3]
