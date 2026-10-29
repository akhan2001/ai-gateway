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
    "session_id",
    "step_name",
    "session_name",
    "session_status",
    "auto_generated",
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
    # Token anatomy (sql/008). Nullable: older rows and error rows have none.
    "system_tokens",
    "tool_tokens",
    "history_tokens",
    "user_tokens",
    "tool_count",
    "cache_write_tokens",
    "uncached_input_tokens",
    "max_output_tokens",
    "prefix_hash",
    "uncached_input_cost_usd",
    "cache_write_cost_usd",
    "cache_read_cost_usd",
    "output_cost_usd",
    "prefix_cache_saving_usd",
    "cost_method_version",
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
    session_id: str | None
    step_name: str | None
    session_name: str | None
    session_status: str | None
    auto_generated: bool

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

    # Counts and one salted hash only. Never message text.
    system_tokens: int | None = None
    tool_tokens: int | None = None
    history_tokens: int | None = None
    user_tokens: int | None = None
    tool_count: int | None = None
    cache_write_tokens: int | None = None
    uncached_input_tokens: int | None = None
    max_output_tokens: int | None = None
    prefix_hash: str | None = None
    uncached_input_cost_usd: float | None = None
    cache_write_cost_usd: float | None = None
    cache_read_cost_usd: float | None = None
    output_cost_usd: float | None = None
    prefix_cache_saving_usd: float | None = None
    #: services/cost.py COST_METHOD_VERSION. NULL on rows from before sql/010.
    cost_method_version: int | None = None

    def as_tuple(self) -> tuple[Any, ...]:
        return tuple(getattr(self, column) for column in _FIELD_ORDER)


# `timestamp` is the only column whose name differs from the attribute, and it
# does not — kept explicit so a column reorder in SQL fails loudly here.
_FIELD_ORDER = LEDGER_COLUMNS
