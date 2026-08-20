"""Tokenix gateway entry point."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx
from fastapi import FastAPI

from .config import settings
from .middleware.auth import Authenticator
from .routers import admin, health, proxy
from .services.acpi import AcpiCatalog
from .services.db import Database
from .services.encrypt import EncryptionNotConfigured, decrypt, encrypt

log = logging.getLogger(__name__)


async def _connect_redis() -> object | None:
    if not settings.redis_url:
        return None
    try:
        import redis.asyncio as aioredis
    except ImportError:
        log.warning("REDIS_URL is set but the redis package is not installed")
        return None
    try:
        client = aioredis.from_url(settings.redis_url, decode_responses=True)
        await client.ping()
        log.info("redis cache connected")
        return client
    except Exception:
        log.warning("redis unreachable; using the in-process key cache", exc_info=True)
        return None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )

    # Fail fast rather than booting a gateway that cannot read the provider
    # credentials it needs on the very first request.
    try:
        encrypt("startup-probe")
    except EncryptionNotConfigured as exc:
        raise RuntimeError(str(exc)) from exc

    app.state.acpi = AcpiCatalog(settings.acpi_prices_path, settings.acpi_refresh_seconds)
    app.state.acpi.start()

    app.state.db = Database(
        settings.asyncpg_dsn,
        queue_capacity=settings.write_queue_capacity,
        batch_size=settings.write_batch_size,
        flush_seconds=settings.write_flush_seconds,
    )
    await app.state.db.start()

    app.state.redis = await _connect_redis()
    app.state.auth = Authenticator(app.state.db, app.state.redis)

    app.state.http = httpx.AsyncClient(
        timeout=httpx.Timeout(
            settings.provider_timeout_seconds,
            connect=settings.provider_connect_timeout_seconds,
        ),
        follow_redirects=False,
    )
    app.state.encrypt = encrypt
    app.state.decrypt = decrypt

    log.info("tokenix gateway ready")
    try:
        yield
    finally:
        await app.state.http.aclose()
        if app.state.redis is not None:
            await app.state.redis.aclose()
        await app.state.db.stop()
        await app.state.acpi.stop()
        log.info("tokenix gateway stopped")


app = FastAPI(
    title="Tokenix Gateway",
    description="OpenAI-compatible proxy that prices every request against the ACPI.",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(health.router)
app.include_router(admin.router)
# Registered last: its `/{provider}/{path:path}` pattern would otherwise
# swallow the more specific routes above.
app.include_router(proxy.router)
