"""Provider adapter interface.

The customer always speaks the **OpenAI wire format** — that is the whole point
of the one-line integration: they keep their existing OpenAI SDK. The URL path
selects the provider, and the adapter translates in both directions:

    OpenAI-shaped request  --build_request-->  provider-shaped request
    provider response      --parse_response->  OpenAI-shaped response
    provider SSE chunk     --parse_stream_chunk-> OpenAI-shaped SSE chunk

Adding a provider is one new file plus a line in `registry.py`.
"""

from __future__ import annotations

import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ..services.usage import Usage


@dataclass
class TranslatedRequest:
    """Everything needed to issue the upstream call."""

    url: str
    payload: dict[str, Any]
    headers: dict[str, str]


@dataclass
class StreamState:
    """Carried across chunks of one streaming response.

    Providers report usage differently across a stream — Anthropic sends input
    tokens up front and output tokens at the end, OpenAI sends everything in a
    single final chunk — so the accumulated total lives here rather than in the
    adapter, which must stay stateless and shareable across requests.
    """

    usage: Usage = field(default_factory=Usage)
    model: str | None = None
    response_id: str = field(default_factory=lambda: f"chatcmpl-{uuid.uuid4().hex[:24]}")
    created: int = field(default_factory=lambda: int(time.time()))


class ProviderAdapter(ABC):
    """Base class for all providers."""

    #: Registry key and the first path segment, e.g. `/openai/v1/...`.
    name: str
    #: Upstream API root, no trailing slash.
    base_url: str

    # -- request ------------------------------------------------------------

    @abstractmethod
    def build_request(
        self, path: str, payload: dict[str, Any], api_key: str
    ) -> TranslatedRequest:
        """Translate an OpenAI-shaped request into this provider's format.

        `path` is everything after the provider prefix, e.g.
        `v1/chat/completions`.
        """

    # -- response -----------------------------------------------------------

    @abstractmethod
    def parse_response(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Translate a buffered provider response into OpenAI shape."""

    @abstractmethod
    def parse_stream_chunk(
        self, event: str | None, data: dict[str, Any], state: StreamState
    ) -> dict[str, Any] | None:
        """Translate one SSE payload into an OpenAI chunk.

        Returns ``None`` for provider events that have no OpenAI equivalent and
        should be dropped from the stream. Implementations must also fold any
        usage they see into ``state.usage``.
        """

    # -- metadata -----------------------------------------------------------

    @abstractmethod
    def extract_usage(self, raw: dict[str, Any]) -> Usage:
        """Token usage from a buffered provider response."""

    def resolve_model(self, payload: dict[str, Any]) -> str:
        """Model id as the provider will see it.

        Strips a `provider/` prefix so `openai/gpt-4o` and `gpt-4o` both work.
        """
        model = payload.get("model")
        if not isinstance(model, str) or not model:
            return "unknown"
        head, sep, tail = model.partition("/")
        if sep and head.lower() == self.name:
            return tail
        return model

    @staticmethod
    def wants_stream(payload: dict[str, Any]) -> bool:
        return bool(payload.get("stream"))
