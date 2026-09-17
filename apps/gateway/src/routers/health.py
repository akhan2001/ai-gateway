"""Liveness and readiness."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..config import settings
from ..services.ratelimit import client_ip, rate_limited_response

router = APIRouter()


@router.get("/health")
async def health(request: Request) -> JSONResponse:
    """Readiness: reports degraded if a dependency is down, but never 500s."""
    app = request.app

    if app.state.ratelimit is not None:
        limit_result = await app.state.ratelimit.check(
            "public", client_ip(request), settings.rate_limit_public_ip_per_minute
        )
        if not limit_result.allowed:
            return rate_limited_response(limit_result.retry_after_seconds)

    acpi = app.state.acpi

    db_ok = False
    if app.state.db.pool is not None:
        try:
            await app.state.db.pool.fetchval("SELECT 1")
            db_ok = True
        except Exception:
            db_ok = False

    acpi_ok = acpi.model_count > 0 and acpi.market_rate_per_million is not None
    ready = db_ok and acpi_ok

    return JSONResponse(
        status_code=200 if ready else 503,
        content={
            "status": "ok" if ready else "degraded",
            "database": "up" if db_ok else "down",
            "acpi": {
                "models": acpi.model_count,
                "market_rate_usd_per_million": acpi.market_rate_per_million,
                "dataset_version": acpi.version.isoformat() if acpi.version else None,
            },
            "ledger": app.state.db.stats.as_dict(),
        },
    )


@router.get("/", response_model=None)
async def root(request: Request) -> dict[str, str] | JSONResponse:
    app = request.app
    if app.state.ratelimit is not None:
        limit_result = await app.state.ratelimit.check(
            "public", client_ip(request), settings.rate_limit_public_ip_per_minute
        )
        if not limit_result.allowed:
            return rate_limited_response(limit_result.retry_after_seconds)

    return {
        "service": "tokenix-gateway",
        "docs": "https://tokenixindex.com/docs",
        "usage": "POST /{provider}/v1/chat/completions with Authorization: Bearer txk-...",
    }
