"""Tokenix analytics API — reads the ledger, serves the dashboard."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .budget_checker import budget_checker
from .db import db
from .migrations import run_startup_migrations
from .routes import benchmark, budget, export, forecast, summary, usage, workspaces

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )
    await db.start()
    await run_startup_migrations()
    budget_checker.start()
    log.info("analytics api ready")
    try:
        yield
    finally:
        await budget_checker.stop()
        await db.stop()


app = FastAPI(title="Tokenix Analytics API", version="0.1.0", lifespan=lifespan)

# The dashboard calls this from the browser, so its origin must be allowed.
# Defaults to localhost for dev; set ALLOWED_ORIGINS in production.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        origin.strip()
        for origin in os.getenv("ALLOWED_ORIGINS", "http://localhost:3000").split(",")
        if origin.strip()
    ],
    allow_credentials=True,
    allow_methods=["GET"],
    allow_headers=["authorization", "content-type"],
)

app.include_router(summary.router)
app.include_router(usage.router)
app.include_router(benchmark.router)
app.include_router(forecast.router)
app.include_router(budget.router)
app.include_router(export.router)
# Server-to-server only: gated by INTERNAL_API_TOKEN, not a txk- key, and
# deliberately outside the CORS allowlist below (no browser calls it).
app.include_router(workspaces.router)


@app.get("/health")
async def health() -> dict:
    try:
        assert db.pool is not None
        await db.pool.fetchval("SELECT 1")
        return {"status": "ok", "database": "up"}
    except Exception:
        return {"status": "degraded", "database": "down"}
