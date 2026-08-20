"""Google Gemini adapter — translates to and from `generateContent`."""

from __future__ import annotations

from typing import Any

from ..services.usage import Usage, usage_from_obj
from .base import ProviderAdapter, StreamState, TranslatedRequest

_FINISH_REASONS = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
    "RECITATION": "content_filter",
}


def _to_contents(messages: list[Any]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Split off the system instruction and map roles to Gemini's vocabulary."""
    system_parts: list[str] = []
    contents: list[dict[str, Any]] = []

    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        content = message.get("content")
        text = content if isinstance(content, str) else _flatten(content)

        if role == "system":
            if text:
                system_parts.append(text)
            continue

        contents.append(
            {
                # Gemini calls the assistant "model" and has no other roles.
                "role": "model" if role == "assistant" else "user",
                "parts": [{"text": text}],
            }
        )

    system_instruction = (
        {"parts": [{"text": "\n\n".join(system_parts)}]} if system_parts else None
    )
    return system_instruction, contents


def _flatten(content: Any) -> str:
    if isinstance(content, list):
        return "".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def _first_text(candidate: dict[str, Any]) -> str:
    parts = (candidate.get("content") or {}).get("parts") or []
    return "".join(part.get("text", "") for part in parts if isinstance(part, dict))


class GoogleAdapter(ProviderAdapter):
    name = "google"
    base_url = "https://generativelanguage.googleapis.com"

    def build_request(
        self, path: str, payload: dict[str, Any], api_key: str
    ) -> TranslatedRequest:
        model = self.resolve_model(payload)
        system_instruction, contents = _to_contents(payload.get("messages") or [])

        body: dict[str, Any] = {"contents": contents}
        if system_instruction:
            body["systemInstruction"] = system_instruction

        generation_config: dict[str, Any] = {}
        if payload.get("temperature") is not None:
            generation_config["temperature"] = payload["temperature"]
        if payload.get("top_p") is not None:
            generation_config["topP"] = payload["top_p"]
        max_tokens = payload.get("max_tokens") or payload.get("max_completion_tokens")
        if max_tokens is not None:
            generation_config["maxOutputTokens"] = max_tokens
        if generation_config:
            body["generationConfig"] = generation_config

        method = "streamGenerateContent" if self.wants_stream(payload) else "generateContent"
        url = f"{self.base_url}/v1beta/models/{model}:{method}"
        if method == "streamGenerateContent":
            url += "?alt=sse"

        return TranslatedRequest(
            url=url,
            payload=body,
            headers={
                # Header auth, so the key never lands in a URL or access log.
                "x-goog-api-key": api_key,
                "content-type": "application/json",
            },
        )

    def parse_response(self, raw: dict[str, Any]) -> dict[str, Any]:
        usage = self.extract_usage(raw)
        candidates = raw.get("candidates") or []
        first = candidates[0] if candidates else {}

        return {
            "id": raw.get("responseId", ""),
            "object": "chat.completion",
            "created": 0,
            "model": raw.get("modelVersion", ""),
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": _first_text(first)},
                    "finish_reason": _FINISH_REASONS.get(
                        first.get("finishReason") or "", "stop"
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
        chunk_usage = usage_from_obj(data)
        if not chunk_usage.is_empty:
            state.usage = state.usage.merge(chunk_usage)
        if isinstance(data.get("modelVersion"), str):
            state.model = data["modelVersion"]

        candidates = data.get("candidates") or []
        if not candidates:
            return None
        first = candidates[0]
        text = _first_text(first)
        finish = first.get("finishReason")

        if not text and not finish:
            return None

        return {
            "id": state.response_id,
            "object": "chat.completion.chunk",
            "created": state.created,
            "model": state.model or "",
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": text} if text else {},
                    "finish_reason": _FINISH_REASONS.get(finish, "stop") if finish else None,
                }
            ],
        }

    def extract_usage(self, raw: dict[str, Any]) -> Usage:
        return usage_from_obj(raw)
