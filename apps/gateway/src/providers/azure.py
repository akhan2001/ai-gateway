"""Azure OpenAI adapter.

Azure OpenAI's *wire format* is the OpenAI format — same request body, same
response shape, same SSE chunks — so, unlike Anthropic/Google, nothing needs
translating there. What's different is routing: a customer's traffic lives
under their own resource (`https://<resource>.openai.azure.com`), authenticates
with an `api-key` header instead of `Authorization: Bearer`, and is addressed
by *deployment name*, not by the model id in the request body — two customers
can both send `"model": "gpt-4o"` and need to land on differently-named
deployments on their own resource. The customer's own model->deployment
mapping is therefore required routing information, not just an api key,
which is why this adapter takes `config` (see `ProviderAdapter.build_request`
and `provider_keys.config` in the schema) where OpenAI/Anthropic/Google don't.
"""

from __future__ import annotations

from typing import Any

from ..services.usage import Usage, usage_from_obj
from .base import ProviderAdapter, StreamState, TranslatedRequest

# A GA chat-completions api-version. Overridable per workspace via
# config["api_version"] for anyone pinned to something newer/older.
DEFAULT_API_VERSION = "2024-10-21"


class AzureConfigError(ValueError):
    """Raised when a workspace's Azure connection metadata is missing or
    incomplete. Callers should turn this into a 400, not a 500 — it means
    the customer hasn't finished setting up Azure on the Connect page, not
    that anything is broken."""


class AzureAdapter(ProviderAdapter):
    name = "azure"
    # No fixed base_url: each workspace calls its own Azure resource.
    base_url = ""

    def build_request(
        self,
        path: str,
        payload: dict[str, Any],
        api_key: str,
        config: dict[str, Any] | None = None,
    ) -> TranslatedRequest:
        config = config or {}
        resource_name = config.get("resource_name")
        if not resource_name:
            raise AzureConfigError(
                "No Azure resource configured for this workspace. Add your "
                "resource name on the Connect page before sending Azure traffic."
            )

        model = self.resolve_model(payload)
        deployments = config.get("deployments") or {}
        # Falls back to the model id itself: naming an Azure deployment after
        # its base model (e.g. a deployment literally called "gpt-4o-mini")
        # is the common convention, and covers the workspace with no explicit
        # mapping configured.
        deployment = deployments.get(model, model)
        api_version = config.get("api_version") or DEFAULT_API_VERSION

        body = dict(payload)
        # The deployment in the URL already pins the model; Azure ignores (and
        # some deployments reject) a "model" field that doesn't match it.
        body.pop("model", None)

        if self.wants_stream(body):
            options = dict(body.get("stream_options") or {})
            options["include_usage"] = True
            body["stream_options"] = options

        url = (
            f"https://{resource_name}.openai.azure.com/openai/deployments/"
            f"{deployment}/chat/completions?api-version={api_version}"
        )

        return TranslatedRequest(
            url=url,
            payload=body,
            headers={"api-key": api_key, "content-type": "application/json"},
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
