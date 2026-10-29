"""Turn token counts into money.

Two numbers come out of here and they must not be conflated:

* ``cost_usd`` — what the request actually cost at the model's list price,
  **net of caching**: uncached input, cache writes, cache reads and output are
  each priced at their own rate. Same formulas as demos/compression-lab.
* ``acpi_bench_usd`` — what the *same token volume* would cost at the
  market-wide ACPI rate (the headline "$ per 1M Standard Compute Units").

Benchmarking a model against its own list price would make overpayment
identically zero on every request, which is why the market rate — not the
per-model rate — is the comparison basis.

This module is the only place cache pricing lives. The analytics API reads the
per-part costs written to the ledger instead of re-deriving them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from .acpi import AcpiCatalog

log = logging.getLogger(__name__)

#: Written on every ledger row, so a chart can tell repricing from usage.
#: 1: every input token at the full input rate (before 2026-10-21).
#: 2: net of caching, each billed part at its own rate (below).
#: Bump it, and document the change in tokenix docs/cost-methodology.md,
#: whenever the formula or the cache rates change what a request costs.
COST_METHOD_VERSION = 2

#: Cache rates are multipliers of the model's input price. The ACPI catalog
#: carries input/output prices only, so these come from the providers' own
#: pricing pages, checked on this date:
#:   platform.claude.com/docs/en/about-claude/pricing
#:   developers.openai.com/api/docs/pricing
#:   ai.google.dev/gemini-api/docs/pricing
#: Re-check them when a provider ships a model family.
CACHE_PRICING_AS_OF = "2026-10-21"

# (read multiplier, write multiplier). Anthropic's write is the 5-minute TTL
# (the gateway never sends cache_control, so 1-hour writes can't occur yet).
# OpenAI and Gemini cache implicitly with no write premium.
_PROVIDER_CACHE_RATES: dict[str, tuple[float, float]] = {
    "anthropic": (0.10, 1.25),
    "openai": (0.10, 1.0),  # gpt-5.x, gpt-6.x, chat-latest
    # ponytail: assumes Azure bills cached input like OpenAI; not checked against Azure's page.
    "azure": (0.10, 1.0),
    "google": (0.10, 1.0),  # every Gemini model listed
}

# Model-family overrides, longest prefix wins. ponytail: prefix table by hand;
# move into the ACPI dataset if the catalog ever carries cache prices.
_MODEL_CACHE_RATES: dict[str, tuple[float, float]] = {
    "claude-fable-5-1": (0.025, 1.25),
    "claude-mythos-5-1": (0.025, 1.25),
    "claude-opus-5-5": (0.05, 1.25),
    "claude-sonnet-5-5": (0.05, 1.25),
    "gpt-6.1-sol": (0.05, 1.0),
    "gpt-4.1": (0.25, 1.0),
    "gpt-4o": (0.50, 1.0),
    "o1": (0.50, 1.0),
    "o3": (0.25, 1.0),
    "o3-mini": (0.50, 1.0),
    "o4-mini": (0.25, 1.0),
}


def cache_rates(provider: str, model_id: str) -> tuple[float, float]:
    """(read, write) multipliers of the input price for this model."""
    matches = [prefix for prefix in _MODEL_CACHE_RATES if model_id.startswith(prefix)]
    if matches:
        return _MODEL_CACHE_RATES[max(matches, key=len)]
    return _PROVIDER_CACHE_RATES.get(provider, (1.0, 1.0))


@dataclass(frozen=True)
class Priced:
    cost_usd: float
    acpi_bench_usd: float
    overpay_usd: float
    acpi_score: float | None
    dataset_version: datetime | None
    #: False when the model was absent from the ACPI dataset and priced at 0.
    #: Analytics must exclude these rather than report them as free.
    priced: bool
    # The billed parts; cost_usd is their sum.
    uncached_input_cost_usd: float = 0.0
    cache_write_cost_usd: float = 0.0
    cache_read_cost_usd: float = 0.0
    output_cost_usd: float = 0.0
    #: What caching the given prefix tokens would save on this request, at
    #: this model's read rate. Feeds the "enable caching" recommendation.
    prefix_cache_saving_usd: float = 0.0

    @property
    def overpay_pct(self) -> float | None:
        if not self.acpi_bench_usd:
            return None
        return (self.overpay_usd / self.acpi_bench_usd) * 100.0


def price_request(
    catalog: AcpiCatalog,
    provider: str,
    model_id: str,
    input_tokens: int,
    output_tokens: int,
    *,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    uncached_prefix_tokens: int = 0,
) -> Priced:
    """`input_tokens` is the whole prompt; the cache counts are parts of it."""
    entry = catalog.lookup(provider, model_id)

    if entry is None:
        log.debug("no ACPI entry for %s/%s; recording unpriced", provider, model_id)
        return Priced(0.0, 0.0, 0.0, None, catalog.version, priced=False)

    read_rate, write_rate = cache_rates(provider, model_id)
    per_input = entry.input_per_million / 1_000_000
    uncached = max(input_tokens - cache_read_tokens - cache_write_tokens, 0)

    uncached_cost = uncached * per_input
    write_cost = cache_write_tokens * per_input * write_rate
    read_cost = cache_read_tokens * per_input * read_rate
    output_cost = output_tokens * entry.output_per_million / 1_000_000
    cost = uncached_cost + write_cost + read_cost + output_cost

    market_rate = catalog.market_rate_per_million
    benchmark = (
        0.0 if market_rate is None else ((input_tokens + output_tokens) * market_rate) / 1_000_000
    )

    return Priced(
        cost_usd=cost,
        acpi_bench_usd=benchmark,
        overpay_usd=cost - benchmark,
        acpi_score=entry.acpi_score,
        dataset_version=catalog.version,
        priced=True,
        uncached_input_cost_usd=uncached_cost,
        cache_write_cost_usd=write_cost,
        cache_read_cost_usd=read_cost,
        output_cost_usd=output_cost,
        prefix_cache_saving_usd=uncached_prefix_tokens * per_input * (1 - read_rate),
    )


def input_saving_usd(
    catalog: AcpiCatalog,
    provider: str,
    model_id: str,
    spans: tuple[tuple[float, float, float], ...],
    *,
    cache_read_tokens: int,
    cache_write_tokens: int,
) -> float | None:
    """What removing tokens from a prompt saves, net of caching, or None if unpriced.

    `spans` is (start, end, saved) per changed message: its token span in the
    original prompt and the tokens removed from it. The provider caches a
    prefix, so position decides the rate: the first `cache_read_tokens` were
    billed at the read rate, the next `cache_write_tokens` at the write rate,
    the rest at the full input rate. Saved tokens are spread evenly over their
    span and priced at the rates of the positions they occupied.
    """
    entry = catalog.lookup(provider, model_id)
    if entry is None:
        return None
    read_rate, write_rate = cache_rates(provider, model_id)
    cached_end = cache_read_tokens + cache_write_tokens
    bands = (
        (0, cache_read_tokens, read_rate),
        (cache_read_tokens, cached_end, write_rate),
        (cached_end, float("inf"), 1.0),
    )
    weighted = 0.0
    for start, end, saved in spans:
        if end <= start:
            continue
        for low, high, rate in bands:
            overlap = max(0.0, min(end, high) - max(start, low))
            weighted += saved * overlap / (end - start) * rate
    return weighted * entry.input_per_million / 1_000_000
