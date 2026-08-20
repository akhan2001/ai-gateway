"""OpenAI adapter — pure passthrough.

The customer already speaks this format, so nothing is translated. The only
work is swapping in the workspace's provider credential and making sure usage
is reported on streaming responses.
"""

from __future__ import annotations

from typing import Any

from ..services.usage import Usage, usage_from_obj
from .base import ProviderAdapter, StreamState, TranslatedRequest


class OpenAIAdapter(ProviderAdapter):
    name = "openai"
    base_url = "https://api.openai.com"

    def build_request(
        self, path: str, payload: dict[str, Any], api_key: str
    ) -> TranslatedRequest:
        body = dict(payload)
        body["model"] = self.resolve_model(payload)

        # OpenAI omits usage from streamed responses unless asked. Without this
        # every streamed request would be logged as costing nothing.
        if self.wants_stream(body):
            options = dict(body.get("stream_options") or {})
            options["include_usage"] = True
            body["stream_options"] = options

        return TranslatedRequest(
            url=f"{self.base_url}/{path.lstrip('/')}",
            payload=body,
            headers={
                "authorization": f"Bearer {api_key}",
                "content-type": "application/json",
            },
        )

    def parse_response(self, raw: dict[str, Any]) -> dict[str, Any]:
        return raw

    def parse_stream_chunk(
        self, event: str | None, data: dict[str, Any], state: StreamState
    ) -> dict[str, Any] | None:
        chunk_usage = usage_from_obj(data)
        if not chunk_usage.is_empty:
            state.usage = state.usage.merge(chunk_usage)
        if isinstance(data.get("model"), str):
            state.model = data["model"]
        return data

    def extract_usage(self, raw: dict[str, Any]) -> Usage:
        return usage_from_obj(raw)
