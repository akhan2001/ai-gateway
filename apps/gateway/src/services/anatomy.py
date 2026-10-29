"""Token anatomy: how a request's input splits into system / tools / history / latest turn.

Content-free by construction. Only character counts, a tool count and one
salted hash leave this module; no message text is kept, logged, or returned.

Method: the provider reports one input total. Each section's share of that
total is estimated from its share of the request's serialized characters, then
scaled so the four sections sum exactly to the provider's number. That costs
one ``json.dumps`` per section (well under 1 ms on a 100 KB request) instead
of a token-counting round trip per request. The split is an estimate; the
total is the provider's.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from ..config import settings

log = logging.getLogger(__name__)

#: Stored with every prefix_hash; hashes of different versions never match.
#: 1: SHA-256 of workspace id + prefix. Guessable: anyone can hash a common
#:    system prompt and look for it. Nulled out by sql/012 once 30 days old.
#: 2: HMAC-SHA256 of the prefix under a per-workspace key derived from
#:    PREFIX_HASH_SECRET. Not computable without the secret.
PREFIX_HASH_VERSION = 2

_SECRET = settings.prefix_hash_secret.encode()
if not _SECRET:
    log.warning("PREFIX_HASH_SECRET is not set; prefix hashes will not be recorded")


def prefix_hash(workspace_id: UUID, prefix: str) -> str | None:
    if not _SECRET:
        return None
    tenant_key = hmac.new(_SECRET, workspace_id.bytes, hashlib.sha256).digest()
    return hmac.new(tenant_key, prefix.encode(), hashlib.sha256).hexdigest()[:16]

_SYSTEM_ROLES = {"system", "developer"}


@dataclass(frozen=True)
class Anatomy:
    system_chars: int
    tool_chars: int
    history_chars: int
    user_chars: int
    tool_count: int
    #: Salted hash of system + tools: equal across requests that share a cacheable prefix.
    prefix_hash: str | None
    max_output_tokens: int | None


def _size(value: Any) -> int:
    if not value:
        return 0
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False))


def sections(payload: dict[str, Any], *, forwards_tools: bool) -> tuple[list[Any], list[Any], list[Any], list[Any]]:
    """(system, tools, history, latest turn) of an OpenAI-shaped request. The
    one definition of the split, shared by the estimate and services/tokenize."""
    messages = [m for m in payload.get("messages") or [] if isinstance(m, dict)]
    system = [m for m in messages if m.get("role") in _SYSTEM_ROLES]
    conversation = [m for m in messages if m.get("role") not in _SYSTEM_ROLES]

    # The latest turn is everything after the last assistant message: the new
    # user message, or the tool results an agent loop is sending back.
    last_assistant = max((i for i, m in enumerate(conversation) if m.get("role") == "assistant"), default=-1)
    history, latest = conversation[: last_assistant + 1], conversation[last_assistant + 1 :]

    tools = (payload.get("tools") or payload.get("functions") or []) if forwards_tools else []
    if not isinstance(tools, list):
        tools = []
    return system, tools, history, latest


def measure(payload: dict[str, Any], workspace_id: UUID, *, forwards_tools: bool) -> Anatomy:
    """Measure an OpenAI-shaped request. `forwards_tools` is False for adapters
    that drop tool definitions, since the provider never billed for them."""
    system, tools, history, latest = sections(payload, forwards_tools=forwards_tools)
    prefix = json.dumps([system, tools], separators=(",", ":"), sort_keys=True, ensure_ascii=False)
    hashed = prefix_hash(workspace_id, prefix) if system or tools else None

    max_tokens = payload.get("max_completion_tokens") or payload.get("max_tokens")
    return Anatomy(
        system_chars=_size(system),
        tool_chars=_size(tools),
        history_chars=_size(history),
        user_chars=_size(latest),
        tool_count=len(tools),
        prefix_hash=hashed,
        max_output_tokens=max_tokens if isinstance(max_tokens, int) and not isinstance(max_tokens, bool) else None,
    )


#: Per-section tokens-per-character relative to the system prompt, from
#: scripts/calibrate_anthropic_split.py (system, tools, history, latest). JSON
#: tool schemas and prose tokenize at different rates, so equal weights
#: misattribute tokens between sections. All 1.0 until the script has been run.
ANTHROPIC_SECTION_WEIGHTS: tuple[float, float, float, float] = (1.0, 1.0, 1.0, 1.0)


def split_method(provider: str) -> str:
    """How the in-request split was made: 'calibrated' when correction weights
    apply, else 'estimate'. services/tokenize upgrades rows to 'tokenized'."""
    return "calibrated" if provider == "anthropic" and set(ANTHROPIC_SECTION_WEIGHTS) != {1.0} else "estimate"


def split_tokens(anatomy: Anatomy, input_tokens: int, provider: str = "") -> tuple[int, int, int, int]:
    """(system, tools, history, user) tokens, summing exactly to `input_tokens`:
    each section's share of serialized characters, weighted per provider."""
    chars = (anatomy.system_chars, anatomy.tool_chars, anatomy.history_chars, anatomy.user_chars)
    weights = ANTHROPIC_SECTION_WEIGHTS if provider == "anthropic" else (1.0, 1.0, 1.0, 1.0)
    return scale_to_total(tuple(c * w for c, w in zip(chars, weights)), input_tokens)


def scale_to_total(sizes: tuple[float, ...], input_tokens: int) -> tuple[int, int, int, int]:
    """Apportion `input_tokens` over four sections by `sizes`. Largest-remainder
    rounding, so no section drifts by more than one token from its share."""
    total = sum(sizes)
    if input_tokens <= 0 or total == 0:
        return (0, 0, 0, max(input_tokens, 0))
    exact = [c * input_tokens / total for c in sizes]
    parts = [int(x) for x in exact]
    by_remainder = sorted(range(4), key=lambda i: exact[i] - parts[i], reverse=True)
    for i in by_remainder[: input_tokens - sum(parts)]:
        parts[i] += 1
    return parts[0], parts[1], parts[2], parts[3]
