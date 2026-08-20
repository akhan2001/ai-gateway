"""Turn token counts into money.

Two numbers come out of here and they must not be conflated:

* ``cost_usd`` — what the request actually cost at the model's list price.
* ``acpi_bench_usd`` — what the *same token volume* would cost at the
  market-wide ACPI rate (the headline "$ per 1M Standard Compute Units").

Benchmarking a model against its own list price would make overpayment
identically zero on every request, which is why the market rate — not the
per-model rate — is the comparison basis.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from .acpi import AcpiCatalog

log = logging.getLogger(__name__)


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
) -> Priced:
    entry = catalog.lookup(provider, model_id)

    if entry is None:
        log.debug("no ACPI entry for %s/%s; recording unpriced", provider, model_id)
        return Priced(0.0, 0.0, 0.0, None, catalog.version, priced=False)

    cost = (
        input_tokens * entry.input_per_million + output_tokens * entry.output_per_million
    ) / 1_000_000

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
    )
