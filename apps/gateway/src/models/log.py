"""The one row the gateway writes per proxied request."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

# Column order must match the COPY in services/db.py.
LEDGER_COLUMNS = (
    "request_id",
    "timestamp",
    "workspace_id",
    "provider",
    "model_id",
    "feature_tag",
    "workload_tag",
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "cost_usd",
    "acpi_bench_usd",
    "overpay_usd",
    "acpi_score",
    "acpi_dataset_version",
    "latency_ms",
    "status_code",
    "is_stream",
    "priced",
)


@dataclass(frozen=True)
class UsageRecord:
    request_id: UUID
    timestamp: datetime
    workspace_id: UUID

    provider: str
    model_id: str
    feature_tag: str | None
    workload_tag: str | None

    input_tokens: int
    output_tokens: int
    cached_input_tokens: int

    cost_usd: float
    acpi_bench_usd: float
    overpay_usd: float
    acpi_score: float | None
    acpi_dataset_version: datetime | None

    latency_ms: int
    status_code: int
    is_stream: bool
    priced: bool

    def as_tuple(self) -> tuple[Any, ...]:
        return tuple(getattr(self, column) for column in _FIELD_ORDER)


# `timestamp` is the only column whose name differs from the attribute, and it
# does not — kept explicit so a column reorder in SQL fails loudly here.
_FIELD_ORDER = LEDGER_COLUMNS
