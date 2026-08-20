"""Anthropic adapter — translates to and from the Messages API."""

from __future__ import annotations

from typing import Any

from ..services.usage import Usage, usage_from_obj
from .base import ProviderAdapter, StreamState, TranslatedRequest

# Anthropic requires max_tokens; OpenAI treats it as optional. Picking a value
# is unavoidable, so pick a generous one and let the model stop naturally.
DEFAULT_MAX_TOKENS = 4096

_FINISH_REASONS = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
}


def _split_system(messages: list[Any]) -> tuple[str | None, list[dict[str, Any]]]:
    """Anthropic takes the system prompt as a top-level field, not a message."""
    system_parts: list[str] = []
    conversation: list[dict[str, Any]] = []

    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "system":
            content = message.get("content")
            if isinstance(content, str):
                system_parts.append(content)
            elif isinstance(content, list):
                system_parts.extend(
                    part.get("text", "")
                    for part in content
                    if isinstance(part, dict) and part.get("type") == "text"
                )
        else:
            conversation.append(message)

    system = "\n\n".join(p for p in system_parts if p) or None
    return system, conversation


def _text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


class AnthropicAdapter(ProviderAdapter):
    name = "anthropic"
    base_url = "https://api.anthropic.com"
    api_version = "2023-06-01"

    def build_request(
        self, path: str, payload: dict[str, Any], api_key: str
    ) -> TranslatedRequest:
        system, messages = _split_system(payload.get("messages") or [])

        body: dict[str, Any] = {
            "model": self.resolve_model(payload),
            "messages": messages,
            "max_tokens": payload.get("max_tokens")
            or payload.get("max_completion_tokens")
            or DEFAULT_MAX_TOKENS,
        }
        if system:
            body["system"] = system
        for src, dst in (
            ("temperature", "temperature"),
            ("top_p", "top_p"),
            ("stop", "stop_sequences"),
            ("stream", "stream"),
        ):
            if payload.get(src) is not None:
                value = payload[src]
                if dst == "stop_sequences" and isinstance(value, str):
                    value = [value]
                body[dst] = value

        return TranslatedRequest(
            # Anthropic exposes /v1/messages regardless of which OpenAI route
            # the customer's SDK called.
            url=f"{self.base_url}/v1/messages",
            payload=body,
            headers={
                "x-api-key": api_key,
                "anthropic-version": self.api_version,
                "content-type": "application/json",
            },
        )

    def parse_response(self, raw: dict[str, Any]) -> dict[str, Any]:
        usage = self.extract_usage(raw)
        return {
            "id": raw.get("id", ""),
            "object": "chat.completion",
            "created": 0,
            "model": raw.get("model", ""),
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": _text_from_content(raw.get("content")),
                    },
                    "finish_reason": _FINISH_REASONS.get(
                        raw.get("stop_reason") or "", "stop"
                    ),
                }
            ],
            "usage": {
                "prompt_tokens": usage.input_tokens,
                "completion_tokens": usage.output_tokens,
                "total_tokens": usage.input_tokens + usage.output_tokens,
            },
        }

    def parse_stream_chunk(
        self, event: str | None, data: dict[str, Any], state: StreamState
    ) -> dict[str, Any] | None:
        kind = event or data.get("type")

        chunk_usage = usage_from_obj(data)
        if not chunk_usage.is_empty:
            state.usage = state.usage.merge(chunk_usage)

        if kind == "message_start":
            message = data.get("message") or {}
            if isinstance(message.get("model"), str):
                state.model = message["model"]
            return self._chunk(state, delta={"role": "assistant", "content": ""})

        if kind == "content_block_delta":
            delta = data.get("delta") or {}
            text = delta.get("text")
            if not isinstance(text, str) or not text:
                return None
            return self._chunk(state, delta={"content": text})

        if kind == "message_delta":
            stop_reason = (data.get("delta") or {}).get("stop_reason")
            return self._chunk(
                state,
                delta={},
                finish_reason=_FINISH_REASONS.get(stop_reason or "", "stop"),
            )

        # ping, content_block_start/stop, message_stop — nothing to forward.
        return None

    def _chunk(
        self,
        state: StreamState,
        delta: dict[str, Any],
        finish_reason: str | None = None,
    ) -> dict[str, Any]:
        return {
            "id": state.response_id,
            "object": "chat.completion.chunk",
            "created": state.created,
            "model": state.model or "",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }

    def extract_usage(self, raw: dict[str, Any]) -> Usage:
        return usage_from_obj(raw)
