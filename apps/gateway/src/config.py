"""Gateway settings, all sourced from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

_HERE = Path(__file__).resolve()
# From a checkout this file is apps/gateway/src/config.py, so the repo root is
# four levels up. The Docker image copies only `src`, leaving /app/src/config.py
# with no fourth parent — indexing blindly would raise at import and kill the
# container on boot. Fall back to the app directory there; compose sets
# ACPI_PRICES_PATH explicitly anyway.
_REPO_ROOT = _HERE.parents[3] if len(_HERE.parents) > 3 else _HERE.parents[1]


def _env(name: str, default: str) -> str:
    value = os.getenv(name)
    return default if value is None or value.strip() == "" else value


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return default if raw is None or raw.strip() == "" else int(raw)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return default if raw is None or raw.strip() == "" else float(raw)


@dataclass(frozen=True)
class Settings:
    database_url: str = field(
        default_factory=lambda: _env(
            "DATABASE_URL", "postgresql://tokenix:tokenix@localhost:5432/tokenix"
        )
    )
    redis_url: str | None = field(default_factory=lambda: os.getenv("REDIS_URL") or None)

    # Upstash Redis REST API, used for rate limiting only (see
    # services/ratelimit.py). Separate from REDIS_URL/redis.asyncio above,
    # which backs the auth-key cache.
    upstash_redis_rest_url: str | None = field(
        default_factory=lambda: os.getenv("UPSTASH_REDIS_REST_URL") or None
    )
    upstash_redis_rest_token: str | None = field(
        default_factory=lambda: os.getenv("UPSTASH_REDIS_REST_TOKEN") or None
    )

    # Fernet key for provider-credential encryption. There is deliberately no
    # default: booting with a hardcoded key would silently make every stored
    # provider credential readable by anyone with the source.
    encryption_key: str = field(default_factory=lambda: _env("ENCRYPTION_KEY", ""))

    # ACPI dataset produced by the tokenix index pipeline
    # (scripts/export_gateway_prices.py) and synced here hourly.
    acpi_prices_path: Path = field(
        default_factory=lambda: Path(
            _env("ACPI_PRICES_PATH", str(_REPO_ROOT / "data" / "acpi_prices.json"))
        )
    )
    acpi_refresh_seconds: int = field(
        default_factory=lambda: _env_int("ACPI_REFRESH_SECONDS", 3600)
    )

    # Auth cache
    key_cache_ttl_seconds: int = field(
        default_factory=lambda: _env_int("KEY_CACHE_TTL_SECONDS", 300)
    )
    key_cache_max_entries: int = field(
        default_factory=lambda: _env_int("KEY_CACHE_MAX_ENTRIES", 10000)
    )

    # Ledger writer
    write_queue_capacity: int = field(
        default_factory=lambda: _env_int("WRITE_QUEUE_CAPACITY", 8192)
    )
    write_batch_size: int = field(default_factory=lambda: _env_int("WRITE_BATCH_SIZE", 256))
    write_flush_seconds: float = field(
        default_factory=lambda: _env_float("WRITE_FLUSH_SECONDS", 2.0)
    )

    # Upstream provider calls
    provider_timeout_seconds: float = field(
        default_factory=lambda: _env_float("PROVIDER_TIMEOUT_SECONDS", 120.0)
    )
    provider_connect_timeout_seconds: float = field(
        default_factory=lambda: _env_float("PROVIDER_CONNECT_TIMEOUT_SECONDS", 10.0)
    )

    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO"))

    # Rate limits, requests/minute. See services/ratelimit.py.
    rate_limit_workspace_per_minute: int = field(
        default_factory=lambda: _env_int("RATE_LIMIT_WORKSPACE_PER_MINUTE", 60)
    )
    rate_limit_public_ip_per_minute: int = field(
        default_factory=lambda: _env_int("RATE_LIMIT_PUBLIC_IP_PER_MINUTE", 10)
    )
    rate_limit_admin_ip_per_minute: int = field(
        default_factory=lambda: _env_int("RATE_LIMIT_ADMIN_IP_PER_MINUTE", 5)
    )

    @property
    def asyncpg_dsn(self) -> str:
        """asyncpg rejects the SQLAlchemy-style ``postgresql+driver://`` form."""
        dsn = self.database_url
        scheme, _, rest = dsn.partition("://")
        if "+" in scheme:
            dsn = f"{scheme.split('+', 1)[0]}://{rest}"
        return dsn


settings = Settings()
