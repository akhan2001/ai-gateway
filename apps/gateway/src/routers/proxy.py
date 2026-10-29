"""The proxy route — everything the customer's traffic actually touches.

Design rule for this file: **nothing that Tokenix wants to know may delay what
the customer asked for.** Pricing and ledger writes happen after the response
has been handed back, on a fire-and-forget task, and any failure in them is
logged and swallowed.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, AsyncIterator

import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from ..config import settings
from ..models.log import UsageRecord
from ..providers.azure import AzureConfigError
from ..providers.base import ProviderAdapter, StreamState
from ..providers.registry import get_adapter, known_providers
from ..services.acpi import utcnow
from ..services.anatomy import measure, split_tokens
from ..services import shadow
from ..services.cost import input_saving_usd, price_request
from ..services.ratelimit import rate_limited_response
from ..services.usage import Usage, usage_from_obj

log = logging.getLogger(__name__)

router = APIRouter()

# Hop-by-hop and body-framing headers that must not be copied upstream.
_STRIPPED_REQUEST_HEADERS = {
    "host",
    "content-length",
    "connection",
    "keep-alive",
    "transfer-encoding",
    "upgrade",
    "authorization",
    "x-api-key",
    "api-key",
    "accept-encoding",
}


def _error(status: int, message: str, kind: str) -> JSONResponse:
    """OpenAI-shaped error, so customer SDKs surface it normally."""
    return JSONResponse(
        status_code=status, content={"error": {"message": message, "type": kind}}
    )


# Tokenix-Session-Status values that end a session immediately rather than
# leaving it for the session worker's inactivity timeout to catch.
_TERMINAL_SESSION_STATUSES = {"completed", "failed"}


# Width of the auto-grouping window used when a request carries no
# Tokenix-Session header: every unheadered request from the same workspace
# within the same 5-minute slice lands in one auto-generated session, so
# basic session tracking costs a customer zero extra code.
_AUTO_SESSION_WINDOW_SECONDS = 300


@dataclass(frozen=True)
class SessionSignals:
    session_id: str | None
    step_name: str | None
    session_name: str | None
    session_status: str | None  # only ever "completed" or "failed"; else None
    auto_generated: bool


def _session_signals(request: Request, workspace_id: uuid.UUID) -> SessionSignals:
    raw_status = request.headers.get("tokenix-session-status")
    status = raw_status.strip().lower() if raw_status else None

    session_id = request.headers.get("tokenix-session")
    session_name = request.headers.get("tokenix-session-name")
    auto_generated = False
    if not session_id:
        window = int(time.time() // _AUTO_SESSION_WINDOW_SECONDS)
        session_id = f"auto-{workspace_id}-{window}"
        session_name = "Auto session"
        auto_generated = True

    return SessionSignals(
        session_id=session_id,
        step_name=request.headers.get("tokenix-step"),
        session_name=session_name,
        session_status=status if status in _TERMINAL_SESSION_STATUSES else None,
        auto_generated=auto_generated,
    )


@router.post("/{provider}/{path:path}")
async def proxy(provider: str, path: str, request: Request) -> Response:
    started = time.monotonic()
    request_id = uuid.uuid4()
    app = request.app

    adapter = get_adapter(provider)
    if adapter is None:
        return _error(
            404,
            f"Unknown provider '{provider}'. Supported: {', '.join(known_providers())}.",
            "invalid_request_error",
        )

    principal = await app.state.auth.authenticate(request.headers.get("authorization"))
    if principal is None:
        return _error(
            401,
            "Invalid or missing Tokenix API key. Pass it as 'Authorization: Bearer txk-...'.",
            "authentication_error",
        )

    session = _session_signals(request, principal.workspace_id)

    if app.state.ratelimit is not None:
        limit_result = await app.state.ratelimit.check(
            "workspace", str(principal.workspace_id), settings.rate_limit_workspace_per_minute
        )
        if not limit_result.allowed:
            return rate_limited_response(limit_result.retry_after_seconds)

    try:
        payload = json.loads(await request.body() or b"{}")
    except json.JSONDecodeError:
        return _error(400, "Request body is not valid JSON.", "invalid_request_error")
    if not isinstance(payload, dict):
        return _error(400, "Request body must be a JSON object.", "invalid_request_error")

    credential = await _provider_credential(app, principal.workspace_id, adapter.name)
    if credential is None:
        return _error(
            400,
            f"No {adapter.name} credential stored for this workspace. "
            f"Add one on the Connect page before sending {adapter.name} traffic.",
            "invalid_request_error",
        )
    upstream_key, provider_config = credential

    try:
        translated = adapter.build_request(path, payload, upstream_key, provider_config)
    except AzureConfigError as exc:
        return _error(400, str(exc), "invalid_request_error")
    translated.headers.update(_passthrough_headers(request))

    model_id = adapter.resolve_model(payload)
    is_stream = adapter.wants_stream(payload)
    tags = (
        request.headers.get("tokenix-feature"),
        request.headers.get("tokenix-workload"),
    )

    client: httpx.AsyncClient = app.state.http

    if is_stream:
        return await _proxy_stream(
            app, client, adapter, translated, request_id, principal, model_id, tags, session, started,
            payload,
        )
    return await _proxy_buffered(
        app, client, adapter, translated, request_id, principal, model_id, tags, session, started,
        payload,
    )


# ---------------------------------------------------------------------------
# buffered
# ---------------------------------------------------------------------------


async def _proxy_buffered(
    app: Any,
    client: httpx.AsyncClient,
    adapter: ProviderAdapter,
    translated: Any,
    request_id: uuid.UUID,
    principal: Any,
    model_id: str,
    tags: tuple[str | None, str | None],
    session: SessionSignals,
    started: float,
    payload: dict[str, Any],
) -> Response:
    try:
        upstream = await client.post(
            translated.url, json=translated.payload, headers=translated.headers
        )
    except httpx.TimeoutException:
        _record(app, request_id, principal, adapter, model_id, Usage(), tags, session, 504, False, started)
        return _error(504, "Upstream provider timed out.", "api_error")
    except httpx.HTTPError as exc:
        log.warning("upstream request failed: %s", exc)
        _record(app, request_id, principal, adapter, model_id, Usage(), tags, session, 502, False, started)
        return _error(502, "Could not reach the upstream provider.", "api_error")

    latency_ms = int((time.monotonic() - started) * 1000)

    try:
        raw = upstream.json()
    except ValueError:
        # Non-JSON upstream error page — forward it verbatim rather than
        # inventing a shape for it.
        _record(
            app, request_id, principal, adapter, model_id, Usage(), tags, session,
            upstream.status_code, False, started,
        )
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "application/json"),
        )

    if upstream.status_code >= 400:
        _record(
            app, request_id, principal, adapter, model_id, Usage(), tags, session,
            upstream.status_code, False, started,
        )
        return JSONResponse(status_code=upstream.status_code, content=raw)

    usage = adapter.extract_usage(raw)
    body = adapter.parse_response(raw)
    served_model = raw.get("model") or raw.get("modelVersion") or model_id

    _record(
        app, request_id, principal, adapter, served_model, usage, tags, session,
        upstream.status_code, False, started, latency_ms=latency_ms, payload=payload,
    )
    return JSONResponse(status_code=upstream.status_code, content=body)


# ---------------------------------------------------------------------------
# streaming
# ---------------------------------------------------------------------------


async def _proxy_stream(
    app: Any,
    client: httpx.AsyncClient,
    adapter: ProviderAdapter,
    translated: Any,
    request_id: uuid.UUID,
    principal: Any,
    model_id: str,
    tags: tuple[str | None, str | None],
    session: SessionSignals,
    started: float,
    payload: dict[str, Any],
) -> Response:
    state = StreamState(model=model_id)

    async def body() -> AsyncIterator[bytes]:
        status = 200
        buffer = ""
        try:
            async with client.stream(
                "POST", translated.url, json=translated.payload, headers=translated.headers
            ) as upstream:
                status = upstream.status_code
                if status >= 400:
                    detail = await upstream.aread()
                    yield _sse(
                        {"error": {"message": detail.decode("utf-8", "replace"), "type": "api_error"}}
                    )
                    return

                async for piece in upstream.aiter_text():
                    buffer += piece
                    # SSE blocks are separated by a blank line; hold back the
                    # trailing fragment until its terminator arrives.
                    while "\n\n" in buffer:
                        block, buffer = buffer.split("\n\n", 1)
                        for out in _translate_block(adapter, block, state):
                            yield out

                for out in _translate_block(adapter, buffer, state):
                    yield out

            yield b"data: [DONE]\n\n"
        except httpx.HTTPError as exc:
            log.warning("upstream stream failed: %s", exc)
            status = 502
            yield _sse(
                {"error": {"message": "Upstream provider stream failed.", "type": "api_error"}}
            )
        finally:
            # Runs whether the stream completed or the client hung up, so a
            # cancelled stream still bills for the tokens already produced.
            _record(
                app, request_id, principal, adapter, state.model or model_id,
                state.usage, tags, session, status, True, started, payload=payload,
            )

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={"cache-control": "no-cache", "x-tokenix-request-id": str(request_id)},
    )


def _translate_block(
    adapter: ProviderAdapter, block: str, state: StreamState
) -> list[bytes]:
    """Turn one raw SSE block into zero or more OpenAI-shaped chunks."""
    if not block.strip():
        return []

    event: str | None = None
    data_lines: list[str] = []
    for line in block.replace("\r\n", "\n").split("\n"):
        if line.startswith("event:"):
            event = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:") :].strip())

    if not data_lines:
        return []
    payload = "\n".join(data_lines)
    if payload == "[DONE]":
        return []  # emitted once by the caller after the stream closes

    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, dict):
        return []

    chunk = adapter.parse_stream_chunk(event, parsed, state)
    if chunk is None:
        # Still fold in any usage the dropped event carried.
        seen = usage_from_obj(parsed)
        if not seen.is_empty:
            state.usage = state.usage.merge(seen)
        return []
    return [_sse(chunk)]


def _sse(payload: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n".encode()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _passthrough_headers(request: Request) -> dict[str, str]:
    return {
        name: value
        for name, value in request.headers.items()
        if name.lower() not in _STRIPPED_REQUEST_HEADERS
        and not name.lower().startswith("tokenix-")
    }


async def _provider_credential(
    app: Any, workspace_id: uuid.UUID, provider: str
) -> tuple[str, dict[str, Any] | None] | None:
    stored = await app.state.db.provider_credential(workspace_id, provider)
    if stored is None:
        return None
    encrypted, config = stored
    try:
        return app.state.decrypt(encrypted), config
    except ValueError:
        log.error("provider credential for workspace %s could not be decrypted", workspace_id)
        return None


def _record(
    app: Any,
    request_id: uuid.UUID,
    principal: Any,
    adapter: ProviderAdapter,
    model_id: str,
    usage: Usage,
    tags: tuple[str | None, str | None],
    session: SessionSignals,
    status_code: int,
    is_stream: bool,
    started: float,
    latency_ms: int | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    """Price and enqueue one ledger row. Never raises, and never records content:
    only the payload's section sizes and a salted prefix hash are kept."""
    try:
        sections: tuple[int, int, int, int] | None = None
        anatomy = None
        if payload is not None and usage.input_tokens > 0:
            anatomy = measure(payload, principal.workspace_id, forwards_tools=adapter.forwards_tools)
            sections = split_tokens(anatomy, usage.input_tokens)
        prefix_tokens = sections[0] + sections[1] if sections else 0
        priced = price_request(
            app.state.acpi, adapter.name, model_id, usage.input_tokens, usage.output_tokens,
            cache_read_tokens=usage.cached_input_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            uncached_prefix_tokens=max(prefix_tokens - usage.cached_input_tokens, 0),
        )
        has_parts = priced.priced and sections is not None
        feature_tag, workload_tag = tags
        record = UsageRecord(
            request_id=request_id,
            timestamp=utcnow(),
            workspace_id=principal.workspace_id,
            provider=adapter.name,
            model_id=model_id,
            feature_tag=feature_tag,
            workload_tag=workload_tag,
            session_id=session.session_id,
            step_name=session.step_name,
            session_name=session.session_name,
            session_status=session.session_status,
            auto_generated=session.auto_generated,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cached_input_tokens=usage.cached_input_tokens,
            cost_usd=priced.cost_usd,
            acpi_bench_usd=priced.acpi_bench_usd,
            overpay_usd=priced.overpay_usd,
            acpi_score=priced.acpi_score,
            acpi_dataset_version=priced.dataset_version,
            latency_ms=latency_ms
            if latency_ms is not None
            else int((time.monotonic() - started) * 1000),
            status_code=status_code,
            is_stream=is_stream,
            priced=priced.priced,
            system_tokens=sections[0] if sections else None,
            tool_tokens=sections[1] if sections else None,
            history_tokens=sections[2] if sections else None,
            user_tokens=sections[3] if sections else None,
            tool_count=anatomy.tool_count if anatomy else None,
            cache_write_tokens=usage.cache_write_tokens if sections else None,
            uncached_input_tokens=usage.uncached_input_tokens if sections else None,
            max_output_tokens=anatomy.max_output_tokens if anatomy else None,
            prefix_hash=anatomy.prefix_hash if anatomy else None,
            uncached_input_cost_usd=priced.uncached_input_cost_usd if has_parts else None,
            cache_write_cost_usd=priced.cache_write_cost_usd if has_parts else None,
            cache_read_cost_usd=priced.cache_read_cost_usd if has_parts else None,
            output_cost_usd=priced.output_cost_usd if has_parts else None,
            prefix_cache_saving_usd=priced.prefix_cache_saving_usd if has_parts else None,
        )
        # Past the in-flight cap the row is written without an estimate, so
        # shadow totals become a sample (an undercount), never a backlog.
        if (
            has_parts
            and principal.compression_shadow
            and not shadow.killed
            and len(_background) < settings.shadow_max_inflight
        ):
            task = asyncio.create_task(_shadow_then_enqueue(app, record, payload, adapter, usage))
            _background.add(task)
            task.add_done_callback(_background.discard)
        else:
            app.state.db.enqueue(record)
    except Exception:
        log.exception("failed to record usage for request %s", request_id)


# Strong refs to in-flight shadow tasks, so the loop can't garbage-collect them.
_background: set[asyncio.Task[None]] = set()


async def _shadow_then_enqueue(
    app: Any, record: UsageRecord, payload: dict[str, Any], adapter: ProviderAdapter, usage: Usage
) -> None:
    """Compression shadow mode. Runs after the response has been handed back,
    on a worker thread so a large request can't stall the event loop, and only
    delays the ledger row, never the customer. The row is written either way."""
    try:
        record = await asyncio.to_thread(_with_shadow, app.state.acpi, record, payload, adapter, usage)
    except Exception as exc:
        # Type only: an exception message could quote the payload.
        log.error("compression shadow failed for request %s: %s", record.request_id, type(exc).__name__)
    app.state.db.enqueue(record)


def _with_shadow(
    catalog: Any, record: UsageRecord, payload: dict[str, Any], adapter: ProviderAdapter, usage: Usage
) -> UsageRecord:
    estimate = shadow.estimate(
        payload,
        usage.input_tokens,
        usage.cached_input_tokens,
        forwards_tools=adapter.forwards_tools,
        dedupe_references=settings.compression_dedupe,
    )
    if estimate is None:
        return record
    saving_usd = input_saving_usd(
        catalog, adapter.name, record.model_id, estimate.spans,
        cache_read_tokens=usage.cached_input_tokens, cache_write_tokens=usage.cache_write_tokens,
    )
    return dataclasses.replace(
        record,
        shadow_compressed_tokens=estimate.compressed_tokens,
        shadow_saving_tokens=estimate.saving_tokens,
        shadow_saving_usd=saving_usd,
        shadow_prefix_changed=estimate.touches_cached_prefix,
    )
