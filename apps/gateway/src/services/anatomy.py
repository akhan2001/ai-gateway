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
import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

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


def measure(payload: dict[str, Any], workspace_id: UUID, *, forwards_tools: bool) -> Anatomy:
    """Measure an OpenAI-shaped request. `forwards_tools` is False for adapters
    that drop tool definitions, since the provider never billed for them."""
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

    prefix = json.dumps([system, tools], separators=(",", ":"), sort_keys=True, ensure_ascii=False)
    prefix_hash = hashlib.sha256(f"{workspace_id}:{prefix}".encode()).hexdigest()[:16] if system or tools else None

    max_tokens = payload.get("max_completion_tokens") or payload.get("max_tokens")
    return Anatomy(
        system_chars=_size(system),
        tool_chars=_size(tools),
        history_chars=_size(history),
        user_chars=_size(latest),
        tool_count=len(tools),
        prefix_hash=prefix_hash,
        max_output_tokens=max_tokens if isinstance(max_tokens, int) and not isinstance(max_tokens, bool) else None,
    )


def split_tokens(anatomy: Anatomy, input_tokens: int) -> tuple[int, int, int, int]:
    """(system, tools, history, user) tokens, summing exactly to `input_tokens`.

    Largest-remainder rounding, so no section drifts by more than one token
    from its proportional share.
    """
    chars = (anatomy.system_chars, anatomy.tool_chars, anatomy.history_chars, anatomy.user_chars)
    total_chars = sum(chars)
    if input_tokens <= 0 or total_chars == 0:
        return (0, 0, 0, max(input_tokens, 0))
    exact = [c * input_tokens / total_chars for c in chars]
    parts = [int(x) for x in exact]
    by_remainder = sorted(range(4), key=lambda i: exact[i] - parts[i], reverse=True)
    for i in by_remainder[: input_tokens - sum(parts)]:
        parts[i] += 1
    return parts[0], parts[1], parts[2], parts[3]
