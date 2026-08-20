"""Token-usage extraction, in whichever dialect the provider reports it.

* OpenAI / OpenAI-compatible: ``usage.prompt_tokens`` / ``completion_tokens``
* Anthropic: ``usage.input_tokens`` / ``output_tokens``
* Google Gemini: ``usageMetadata.promptTokenCount`` / ``candidatesTokenCount``

Token counts are read from what the provider actually reports — never
estimated locally — because the provider's count is what the customer is
billed on, and an approximation would put the whole ledger subtly out of step
with their real invoice.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterator


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0

    @property
    def is_empty(self) -> bool:
        return self.input_tokens == 0 and self.output_tokens == 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    def merge(self, other: "Usage") -> "Usage":
        """Later non-zero values win.

        Streaming responses report input tokens once at the start and output
        tokens as a running total, so summing would double-count and taking a
        max would lose the final value.
        """
        return Usage(
            input_tokens=other.input_tokens or self.input_tokens,
            output_tokens=other.output_tokens or self.output_tokens,
            cached_input_tokens=other.cached_input_tokens or self.cached_input_tokens,
        )


def _as_int(value: Any) -> int:
    if value is None or isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    return 0


def usage_from_obj(obj: Any) -> Usage:
    """Pull usage out of one parsed JSON object, whatever dialect it is in."""
    if not isinstance(obj, dict):
        return Usage()

    # Anthropic's message_start nests the initial usage under `message`.
    message = obj.get("message")
    if isinstance(message, dict) and "usage" in message:
        nested = usage_from_obj(message)
        if not nested.is_empty:
            return nested

    meta = obj.get("usageMetadata")
    if isinstance(meta, dict):
        return Usage(
            input_tokens=_as_int(meta.get("promptTokenCount")),
            output_tokens=_as_int(meta.get("candidatesTokenCount")),
            cached_input_tokens=_as_int(meta.get("cachedContentTokenCount")),
        )

    usage = obj.get("usage")
    if not isinstance(usage, dict):
        return Usage()

    if "prompt_tokens" in usage or "completion_tokens" in usage:
        details = usage.get("prompt_tokens_details")
        cached = _as_int(details.get("cached_tokens")) if isinstance(details, dict) else 0
        return Usage(
            input_tokens=_as_int(usage.get("prompt_tokens")),
            output_tokens=_as_int(usage.get("completion_tokens")),
            cached_input_tokens=cached,
        )

    if "input_tokens" in usage or "output_tokens" in usage:
        return Usage(
            input_tokens=_as_int(usage.get("input_tokens")),
            output_tokens=_as_int(usage.get("output_tokens")),
            cached_input_tokens=_as_int(usage.get("cache_read_input_tokens")),
        )

    return Usage()


def iter_sse_events(text: str) -> Iterator[tuple[str | None, dict[str, Any]]]:
    """Yield ``(event_name, payload)`` for each SSE block in `text`.

    Used by tests and buffered fallbacks; the live proxy path parses
    incrementally in `routers/proxy.py` instead.
    """
    for block in text.replace("\r\n", "\n").split("\n\n"):
        event: str | None = None
        data_lines: list[str] = []
        for line in block.split("\n"):
            if line.startswith("event:"):
                event = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:") :].strip())
        if not data_lines:
            continue
        payload = "\n".join(data_lines)
        if payload == "[DONE]":
            continue
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            yield event, parsed


def extract_usage(body: str) -> Usage:
    """Best-effort usage for a whole response body, buffered or SSE."""
    if not body:
        return Usage()

    if body.lstrip().startswith(("data:", "event:")):
        total = Usage()
        for _, payload in iter_sse_events(body):
            total = total.merge(usage_from_obj(payload))
        return total

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        total = Usage()
        for _, payload in iter_sse_events(body):
            total = total.merge(usage_from_obj(payload))
        return total

    return usage_from_obj(parsed)
