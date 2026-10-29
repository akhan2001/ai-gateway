"""Compression shadow mode: what safe compression WOULD remove from a request.

Nothing here changes what is sent upstream. It runs after the response, on a
worker thread, only for workspaces with ``compression_shadow`` on, and returns
counts only: the compressed messages exist in memory for the length of one
function call and are never stored, logged, or returned.

The transforms are the Compression Lab's (demos/compression-lab/lib/compress.ts),
ported to the OpenAI wire format the gateway speaks:

* minify JSON in tool results (``role: "tool"``)
* normalize whitespace (trailing spaces, runs of blank lines, outer trim)
* only with ``dedupe_references`` (COMPRESSION_DEDUPE): replace a later exact
  duplicate of >= 200 chars with a short reference. The other two keep every
  word; this one removes content the model would have seen.

All are prefix-stable: a message's output depends only on that message and the
ones before it, so appending a turn never rewrites an earlier one and a
provider's prompt cache would stay valid. System/developer messages and tool
definitions are never touched, and unused tools are never removed.

Token counts use the analyzer's method (services/anatomy.py): each message's
share of the request's serialized characters, scaled to the provider's input
total. The saving is an estimate; the total it is scaled to is the provider's.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from ..config import settings
from .anatomy import _SYSTEM_ROLES, _size

#: Below this, a duplicate isn't worth replacing with a reference.
MIN_DEDUPE_CHARS = 200

#: Global kill switch: when True no shadow work runs for any workspace.
#: ponytail: per process, so with several replicas flip each one (or set
#: COMPRESSION_KILL and redeploy); move to Redis/DB if the gateway scales out.
killed: bool = settings.compression_killed

#: Sampled requests skipped because SHADOW_MAX_INFLIGHT was reached. Exposed on
#: /health; since boot, per process.
dropped: int = 0

_TRAILING_SPACE = re.compile(r"[ \t]+$", re.MULTILINE)
_BLANK_RUNS = re.compile(r"\n{3,}")


def normalize_whitespace(text: str) -> str:
    return _BLANK_RUNS.sub("\n\n", _TRAILING_SPACE.sub("", text)).strip()


def _minify_json(text: str) -> str | None:
    trimmed = text.strip()
    if not trimmed.startswith(("{", "[")):
        return None
    try:
        return json.dumps(json.loads(trimmed), separators=(",", ":"), ensure_ascii=False)
    except (ValueError, RecursionError):
        return None


def compress_messages(messages: list[Any], *, dedupe_references: bool = False) -> list[Any]:
    """The compressed copy of `messages`. The input is never mutated."""
    first_seen: dict[str, int] = {}

    def dedupe(text: str, number: int) -> str:
        if not dedupe_references or len(text) < MIN_DEDUPE_CHARS:
            return text
        first = first_seen.get(text)
        if first is not None:
            return f"[Duplicate omitted: identical to content in message {first}.]"
        first_seen[text] = number
        return text

    out: list[Any] = []
    for number, message in enumerate(messages, start=1):
        if not isinstance(message, dict) or message.get("role") in _SYSTEM_ROLES:
            out.append(message)
            continue
        content = message.get("content")
        if isinstance(content, str):
            text = (_minify_json(content) if message.get("role") == "tool" else None) or normalize_whitespace(content)
            message = {**message, "content": dedupe(text, number)}
        elif isinstance(content, list):
            message = {
                **message,
                "content": [
                    {**block, "text": dedupe(normalize_whitespace(block["text"]), number)}
                    if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
                    else block
                    for block in content
                ],
            }
        out.append(message)
    return out


@dataclass(frozen=True)
class Shadow:
    compressed_tokens: int
    saving_tokens: int
    #: Per changed message: (start, end, saved) — its token span in the original
    #: prompt and the tokens compression removes from it. Used for pricing only.
    spans: tuple[tuple[float, float, float], ...]
    #: True when a saving falls inside what the provider served from cache:
    #: switching compression on mid-conversation would cost one cache miss there.
    touches_cached_prefix: bool


def estimate(
    payload: dict[str, Any],
    input_tokens: int,
    cache_read_tokens: int,
    *,
    forwards_tools: bool,
    dedupe_references: bool = False,
) -> Shadow | None:
    messages = payload.get("messages")
    if not isinstance(messages, list) or input_tokens <= 0:
        return None
    system = [m for m in messages if isinstance(m, dict) and m.get("role") in _SYSTEM_ROLES]
    tools = (payload.get("tools") or payload.get("functions") or []) if forwards_tools else []
    conversation = [m for m in messages if not (isinstance(m, dict) and m.get("role") in _SYSTEM_ROLES)]

    original = [_size(m) for m in conversation]
    compressed = [_size(m) for m in compress_messages(conversation, dedupe_references=dedupe_references)]
    total_chars = _size(system) + (_size(tools) if isinstance(tools, list) else 0) + sum(original)
    if total_chars == 0:
        return None
    per_char = input_tokens / total_chars

    # Cacheable order: system and tools first, then the conversation.
    position = (total_chars - sum(original)) * per_char
    spans = []
    for before, after in zip(original, compressed):
        end = position + before * per_char
        if after < before:
            spans.append((position, end, (before - after) * per_char))
        position = end

    saving = min(round(sum(saved for _, _, saved in spans)), input_tokens)
    return Shadow(
        compressed_tokens=input_tokens - saving,
        saving_tokens=saving,
        spans=tuple(spans),
        touches_cached_prefix=any(start < cache_read_tokens for start, _, _ in spans),
    )
