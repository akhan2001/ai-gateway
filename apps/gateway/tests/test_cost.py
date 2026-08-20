"""Pricing and usage-extraction tests — no database, no network."""

from __future__ import annotations

import json

import pytest

from src.services.acpi import AcpiCatalog
from src.services.cost import price_request
from src.services.usage import Usage, extract_usage, usage_from_obj

DATASET = {
    "last_updated": "2026-08-18T17:23:01Z",
    "index": {"acpi_usd_per_million": 5.0},
    "models": {
        "openai/gpt-4o": {
            "input_per_million": 2.5,
            "output_per_million": 10.0,
            "acpi_score": 4.1,
        },
        "deepseek/deepseek-v3": {
            "input_per_million": 0.27,
            "output_per_million": 1.1,
            "acpi_score": 7.2,
        },
    },
}


@pytest.fixture()
def catalog(tmp_path):
    path = tmp_path / "acpi_prices.json"
    path.write_text(json.dumps(DATASET), encoding="utf-8")
    cat = AcpiCatalog(path, refresh_seconds=3600)
    cat.load()
    return cat


def test_catalog_loads(catalog):
    assert catalog.model_count == 2
    assert catalog.market_rate_per_million == 5.0
    assert catalog.version is not None


def test_lookup_by_prefixed_bare_and_variant(catalog):
    assert catalog.lookup("openai", "gpt-4o") is not None
    assert catalog.lookup("openai", "openai/gpt-4o") is not None
    # Variant SKUs price as their base model, matching the index pipeline.
    assert catalog.lookup("openai", "gpt-4o:free") is not None
    assert catalog.lookup("openai", "totally-unknown-model") is None


def test_cost_uses_list_price(catalog):
    priced = price_request(catalog, "openai", "gpt-4o", 1_000_000, 1_000_000)
    assert priced.priced is True
    assert priced.cost_usd == pytest.approx(12.5)


def test_benchmark_uses_market_rate_not_list_price(catalog):
    """The whole product rests on these two numbers differing."""
    priced = price_request(catalog, "openai", "gpt-4o", 1_000_000, 1_000_000)
    # 2M tokens at the $5.00/1M market rate.
    assert priced.acpi_bench_usd == pytest.approx(10.0)
    assert priced.overpay_usd == pytest.approx(2.5)
    assert priced.overpay_pct == pytest.approx(25.0)


def test_cheap_model_shows_negative_overpayment(catalog):
    priced = price_request(catalog, "deepseek", "deepseek-v3", 1_000_000, 1_000_000)
    assert priced.cost_usd == pytest.approx(1.37)
    assert priced.overpay_usd < 0


def test_unknown_model_is_flagged_unpriced_not_free(catalog):
    priced = price_request(catalog, "openai", "gpt-9-turbo", 1000, 1000)
    assert priced.priced is False
    assert priced.cost_usd == 0.0


def test_missing_file_leaves_catalog_empty(tmp_path):
    cat = AcpiCatalog(tmp_path / "nope.json", refresh_seconds=3600)
    cat.load()
    assert cat.model_count == 0
    assert price_request(cat, "openai", "gpt-4o", 10, 10).priced is False


# --- usage extraction --------------------------------------------------------


def test_usage_openai():
    usage = usage_from_obj({"usage": {"prompt_tokens": 12, "completion_tokens": 34}})
    assert (usage.input_tokens, usage.output_tokens) == (12, 34)


def test_usage_anthropic():
    usage = usage_from_obj({"usage": {"input_tokens": 5, "output_tokens": 7}})
    assert (usage.input_tokens, usage.output_tokens) == (5, 7)


def test_usage_gemini():
    usage = usage_from_obj(
        {"usageMetadata": {"promptTokenCount": 9, "candidatesTokenCount": 3}}
    )
    assert (usage.input_tokens, usage.output_tokens) == (9, 3)


def test_usage_anthropic_message_start_nesting():
    usage = usage_from_obj(
        {"type": "message_start", "message": {"usage": {"input_tokens": 40, "output_tokens": 1}}}
    )
    assert usage.input_tokens == 40


def test_merge_prefers_later_nonzero():
    start = Usage(input_tokens=40, output_tokens=1)
    end = Usage(output_tokens=99)
    merged = start.merge(end)
    # Input survives from the first chunk; output is replaced, not summed.
    assert (merged.input_tokens, merged.output_tokens) == (40, 99)


def test_extract_usage_from_sse_stream():
    stream = (
        'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        'data: {"choices":[],"usage":{"prompt_tokens":11,"completion_tokens":22}}\n\n'
        "data: [DONE]\n\n"
    )
    usage = extract_usage(stream)
    assert (usage.input_tokens, usage.output_tokens) == (11, 22)


def test_extract_usage_from_anthropic_sse():
    stream = (
        "event: message_start\n"
        'data: {"type":"message_start","message":{"usage":{"input_tokens":50,"output_tokens":0}}}\n\n'
        "event: message_delta\n"
        'data: {"type":"message_delta","usage":{"output_tokens":25}}\n\n'
    )
    usage = extract_usage(stream)
    assert (usage.input_tokens, usage.output_tokens) == (50, 25)


def test_garbage_body_yields_empty_usage():
    assert extract_usage("not json at all").is_empty
    assert extract_usage("").is_empty
