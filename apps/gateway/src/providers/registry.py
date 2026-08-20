"""Provider name -> adapter instance.

Adapters are stateless and safe to share across requests; anything that varies
per request lives in `StreamState`.
"""

from __future__ import annotations

from .anthropic import AnthropicAdapter
from .base import ProviderAdapter
from .google import GoogleAdapter
from .openai import OpenAIAdapter

_ADAPTERS: dict[str, ProviderAdapter] = {
    adapter.name: adapter
    for adapter in (OpenAIAdapter(), AnthropicAdapter(), GoogleAdapter())
}


def get_adapter(provider: str) -> ProviderAdapter | None:
    return _ADAPTERS.get(provider.strip().lower())


def known_providers() -> list[str]:
    return sorted(_ADAPTERS)
