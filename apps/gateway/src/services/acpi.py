"""ACPI pricing: turn token counts into cost and a market benchmark.

Two different numbers come out of here and they must not be conflated:

* ``cost_usd`` — what the request actually cost, at the model's own list price.
* ``acpi_benchmark_usd`` — what the *same token volume* would cost at the
  market-wide ACPI rate (the headline "$ per 1M Standard Compute Units").

Benchmarking a model against its own list price would make the overpayment
identically zero for every request, which is why the market rate — not the
per-model rate — is the comparison basis.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

# The standard 3:1 input:output usage assumption used throughout the index.
INPUT_BLEND_WEIGHT = 0.75
OUTPUT_BLEND_WEIGHT = 0.25


@dataclass(frozen=True)
class ModelPrice:
    input_per_million: float
    output_per_million: float
    acpi_score: float | None = None

    def blended_per_million(self) -> float:
        return (
            self.input_per_million * INPUT_BLEND_WEIGHT
            + self.output_per_million * OUTPUT_BLEND_WEIGHT
        )


def _normalize(text: str) -> str:
    return text.strip().lower()


def _strip_variant(model_id: str) -> str:
    """Drop OpenRouter-style variant suffixes so ``x:free`` prices as ``x``.

    Mirrors ``get_base_model_id`` in the index pipeline.
    """
    base = model_id.split(":", 1)[0]
    return base.split("~", 1)[0]


class AcpiCatalog:
    """In-memory ACPI dataset with a background reloader.

    Reads are lock-free: the loader swaps whole dicts in, and Python attribute
    assignment is atomic, so a request either sees the old dataset or the new
    one and never a half-applied mix.
    """

    def __init__(self, path: Path, refresh_seconds: int) -> None:
        self._path = path
        self._refresh_seconds = refresh_seconds
        self._models: dict[str, ModelPrice] = {}
        self._by_bare_name: dict[str, ModelPrice] = {}
        self._market_rate_per_million: float | None = None
        self._version: datetime | None = None
        self._task: asyncio.Task[None] | None = None

    # -- lifecycle ----------------------------------------------------------

    def load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            log.error("ACPI price file not found at %s; requests will be unpriced", self._path)
            return
        except json.JSONDecodeError:
            log.exception("ACPI price file at %s is not valid JSON; keeping previous data", self._path)
            return

        models: dict[str, ModelPrice] = {}
        by_bare: dict[str, ModelPrice] = {}
        for key, entry in (raw.get("models") or {}).items():
            try:
                price = ModelPrice(
                    input_per_million=float(entry["input_per_million"]),
                    output_per_million=float(entry["output_per_million"]),
                    acpi_score=(
                        float(entry["acpi_score"]) if entry.get("acpi_score") is not None else None
                    ),
                )
            except (KeyError, TypeError, ValueError):
                log.warning("skipping malformed ACPI entry for %s", key)
                continue

            norm = _normalize(key)
            models[norm] = price
            # Also index by the bare model name so a request that reports
            # `gpt-4o` without a provider prefix still prices.
            bare = norm.split("/", 1)[-1]
            by_bare.setdefault(bare, price)

        index = raw.get("index") or {}
        market_rate = index.get("acpi_usd_per_million")

        version: datetime | None = None
        if raw.get("last_updated"):
            try:
                version = datetime.fromisoformat(str(raw["last_updated"]).replace("Z", "+00:00"))
            except ValueError:
                log.warning("unparseable last_updated in ACPI file: %r", raw.get("last_updated"))

        self._models = models
        self._by_bare_name = by_bare
        self._market_rate_per_million = float(market_rate) if market_rate is not None else None
        self._version = version
        log.info(
            "loaded ACPI dataset: %d models, market rate %s, version %s",
            len(models),
            self._market_rate_per_million,
            version,
        )

    async def _refresh_forever(self) -> None:
        while True:
            await asyncio.sleep(self._refresh_seconds)
            try:
                self.load()
            except Exception:  # never let the reloader die
                log.exception("ACPI refresh failed; keeping previous dataset")

    def start(self) -> None:
        self.load()
        self._task = asyncio.create_task(self._refresh_forever())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    # -- lookup -------------------------------------------------------------

    @property
    def version(self) -> datetime | None:
        return self._version

    @property
    def market_rate_per_million(self) -> float | None:
        return self._market_rate_per_million

    @property
    def model_count(self) -> int:
        return len(self._models)

    def lookup(self, provider: str, model_id: str) -> ModelPrice | None:
        model_norm = _strip_variant(_normalize(model_id))
        provider_norm = _normalize(provider)

        # `openai/gpt-4o`
        hit = self._models.get(f"{provider_norm}/{model_norm}")
        if hit is not None:
            return hit
        # the model id already carried its own prefix
        hit = self._models.get(model_norm)
        if hit is not None:
            return hit
        # bare name, any provider
        return self._by_bare_name.get(model_norm.split("/", 1)[-1])


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
