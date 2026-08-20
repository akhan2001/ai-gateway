"""The proxy route — everything the customer's traffic actually touches.

Design rule for this file: **nothing that Tokenix wants to know may delay what
the customer asked for.** Pricing and ledger writes happen after the response
has been handed back, on a fire-and-forget task, and any failure in them is
logged and swallowed.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, AsyncIterator

import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from ..models.log import UsageRecord
from ..providers.base import ProviderAdapter, StreamState
from ..providers.registry import get_adapter, known_providers
from ..services.acpi import utcnow
from ..services.cost import price_request
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
    "accept-encoding",
}


def _error(status: int, message: str, kind: str) -> JSONResponse:
    """OpenAI-shaped error, so customer SDKs surface it normally."""
    return JSONResponse(
        status_code=status, content={"error": {"message": message, "type": kind}}
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

    try:
        payload = json.loads(await request.body() or b"{}")
    except json.JSONDecodeError:
        return _error(400, "Request body is not valid JSON.", "invalid_request_error")
    if not isinstance(payload, dict):
        return _error(400, "Request body must be a JSON object.", "invalid_request_error")

    upstream_key = await _provider_credential(app, principal.workspace_id, adapter.name)
    if upstream_key is None:
        return _error(
            400,
            f"No {adapter.name} credential stored for this workspace. "
            f"Add one on the Connect page before sending {adapter.name} traffic.",
            "invalid_request_error",
        )

    translated = adapter.build_request(path, payload, upstream_key)
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
            app, client, adapter, translated, request_id, principal, model_id, tags, started
        )
    return await _proxy_buffered(
        app, client, adapter, translated, request_id, principal, model_id, tags, started
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
    started: float,
) -> Response:
    try:
        upstream = await client.post(
            translated.url, json=translated.payload, headers=translated.headers
        )
    except httpx.TimeoutException:
        _record(app, request_id, principal, adapter, model_id, Usage(), tags, 504, False, started)
        return _error(504, "Upstream provider timed out.", "api_error")
    except httpx.HTTPError as exc:
        log.warning("upstream request failed: %s", exc)
        _record(app, request_id, principal, adapter, model_id, Usage(), tags, 502, False, started)
        return _error(502, "Could not reach the upstream provider.", "api_error")

    latency_ms = int((time.monotonic() - started) * 1000)

    try:
        raw = upstream.json()
    except ValueError:
        # Non-JSON upstream error page — forward it verbatim rather than
        # inventing a shape for it.
        _record(
            app, request_id, principal, adapter, model_id, Usage(), tags,
            upstream.status_code, False, started,
        )
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "application/json"),
        )

    if upstream.status_code >= 400:
        _record(
            app, request_id, principal, adapter, model_id, Usage(), tags,
            upstream.status_code, False, started,
        )
        return JSONResponse(status_code=upstream.status_code, content=raw)

    usage = adapter.extract_usage(raw)
    body = adapter.parse_response(raw)
    served_model = raw.get("model") or raw.get("modelVersion") or model_id

    _record(
        app, request_id, principal, adapter, served_model, usage, tags,
        upstream.status_code, False, started, latency_ms=latency_ms,
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
    started: float,
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
                state.usage, tags, status, True, started,
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


async def _provider_credential(app: Any, workspace_id: uuid.UUID, provider: str) -> str | None:
    encrypted = await app.state.db.provider_key(workspace_id, provider)
    if not encrypted:
        return None
    try:
        return app.state.decrypt(encrypted)
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
    status_code: int,
    is_stream: bool,
    started: float,
    latency_ms: int | None = None,
) -> None:
    """Price and enqueue one ledger row. Never raises."""
    try:
        priced = price_request(
            app.state.acpi, adapter.name, model_id, usage.input_tokens, usage.output_tokens
        )
        feature_tag, workload_tag = tags
        app.state.db.enqueue(
            UsageRecord(
                request_id=request_id,
                timestamp=utcnow(),
                workspace_id=principal.workspace_id,
                provider=adapter.name,
                model_id=model_id,
                feature_tag=feature_tag,
                workload_tag=workload_tag,
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
            )
        )
    except Exception:
        log.exception("failed to record usage for request %s", request_id)
