"""Optimize endpoints — no database, no network."""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from src import db as dbmod
from src.auth import current_workspace
from src.main import app
from src.routes.optimize import MIN_REQUESTS, build_anatomy, build_recommendations, build_shadow

WORKSPACE = uuid4()


def group(**overrides):
    """One (feature, step, provider, model) row as the SQL returns it: an agent
    with a big stable prefix, 20 tools, no caching, and a long output tail."""
    row = {
        "feature": "support", "step": "triage", "provider": "openai", "model_id": "gpt-4o",
        "requests": 1000, "span_days": 10.0,
        "input_tokens": 10_000_000, "output_tokens": 500_000,
        "cache_read_tokens": 0, "cache_write_tokens": 0, "uncached_input_tokens": 10_000_000,
        "system_tokens": 1_000_000, "tool_tokens": 4_000_000, "history_tokens": 4_000_000, "user_tokens": 1_000_000,
        "avg_tool_count": Decimal("20"), "distinct_prefixes": 1, "no_output_limit": 1000,
        "uncached_input_usd": Decimal("25.0"), "cache_write_usd": Decimal("0"), "cache_read_usd": Decimal("0"),
        "output_usd": Decimal("5.0"), "prefix_cache_saving_usd": Decimal("6.25"),
        "out_p50": 200.0, "out_p95": 1200.0, "output_tail_tokens": Decimal("40000"),
    }
    row.update(overrides)
    return row


def clean(rows):
    from src.routes.optimize import _clean

    return [_clean(r) for r in rows]


def test_anatomy_shares_and_dollars_by_part():
    out = build_anatomy(clean([group(), group(model_id="gpt-4o-mini", requests=50, input_tokens=0, system_tokens=0,
                                         tool_tokens=0, history_tokens=0, user_tokens=0)]), 30)
    assert out["analyzed_requests"] == 1050
    (step,) = out["steps"]  # two models, one feature/step
    assert step["share"] == {"system": 0.1, "tool": 0.4, "history": 0.4, "user": 0.1}
    assert step["cache_hit_rate"] == 0
    assert step["usd"] == {"uncached_input": 50.0, "cache_write": 0.0, "cache_read": 0.0, "output": 10.0}


def test_recommendations_are_ranked_estimates_with_their_basis():
    out = build_recommendations(clean([group()]), 30)
    kinds = [r["kind"] for r in out["recommendations"]]
    assert set(kinds) == {"enable_caching", "tool_search", "output_limits"}  # history share is 40%, under 50%
    monthly = [r["monthly_usd"] for r in out["recommendations"]]
    assert monthly == sorted(monthly, reverse=True)
    assert all(r["estimate"] is True and r["basis"]["requests"] == 1000 and r["note"] for r in out["recommendations"])

    by_kind = {r["kind"]: r for r in out["recommendations"]}
    # 10 observed days scaled to 30.
    assert by_kind["enable_caching"]["monthly_usd"] == pytest.approx(6.25 * 3)
    # 4M tool tokens × (1 − 5/20) × $2.50/M blended, × 3.
    assert by_kind["tool_search"]["monthly_usd"] == pytest.approx(4_000_000 * 0.75 * 2.5e-6 * 3)
    # 40k tail tokens × $10/M, × 3.
    assert by_kind["output_limits"]["monthly_usd"] == pytest.approx(40_000 * 1e-5 * 3)


def test_rules_stay_quiet_when_the_data_says_so():
    healthy = group(cache_read_tokens=8_000_000, distinct_prefixes=900, tool_tokens=500_000, avg_tool_count=Decimal("3"),
                    no_output_limit=0)
    assert build_recommendations(clean([healthy]), 30)["recommendations"] == []


def test_history_rule():
    long_history = group(history_tokens=7_000_000, tool_tokens=1_000_000, system_tokens=500_000, user_tokens=1_500_000)
    kinds = {r["kind"] for r in build_recommendations(clean([long_history]), 30)["recommendations"]}
    assert "summarize_history" in kinds


def test_anthropic_caching_note_is_honest_about_the_gateway():
    (rec,) = [r for r in build_recommendations(clean([group(provider="anthropic")]), 30)["recommendations"] if r["kind"] == "enable_caching"]
    assert "does not send yet" in rec["note"]


def test_low_volume_groups_are_skipped_not_extrapolated():
    out = build_recommendations(clean([group(requests=MIN_REQUESTS - 1)]), 30)
    assert out["recommendations"] == [] and out["groups_skipped_for_low_volume"] == 1


@pytest.mark.parametrize("rows", [[], [group()]])
def test_endpoints(monkeypatch, rows):
    async def fake_fetch(query: str, *args):
        assert "system_tokens IS NOT NULL" in query
        return rows

    monkeypatch.setattr(dbmod.db, "fetch", fake_fetch)
    app.dependency_overrides[current_workspace] = lambda: WORKSPACE
    try:
        client = TestClient(app)
        anatomy = client.get("/api/v1/optimize/anatomy?days=14").json()
        recs = client.get("/api/v1/optimize/recommendations").json()
    finally:
        app.dependency_overrides.clear()
    assert anatomy["days"] == 14 and anatomy["analyzed_requests"] == sum(r["requests"] for r in rows)
    assert recs["min_requests"] == MIN_REQUESTS
    assert bool(recs["recommendations"]) == bool(rows)


def test_no_prefix_hashes_means_no_caching_claim():
    # PREFIX_HASH_SECRET unset at the gateway: stability is unknown, not perfect.
    kinds = [r["kind"] for r in build_recommendations(clean([group(distinct_prefixes=0)]), 30)["recommendations"]]
    assert "enable_caching" not in kinds


def test_shadow_compares_compression_and_caching_on_the_same_requests():
    rows = [
        {"feature": "support", "step": "triage", "sampled_requests": 12, "requests": Decimal("120.0"),
         "input_tokens": Decimal("1000000.0"), "saving_tokens": Decimal("80000.0"), "compression_usd": Decimal("0.12"),
         "caching_usd": Decimal("1.5"), "prefix_changed_requests": Decimal("30.0"), "unverified_pricing": True},
        {"feature": "support", "step": "reply", "sampled_requests": 40, "requests": Decimal("40"),
         "input_tokens": Decimal("200000"), "saving_tokens": Decimal("0"), "compression_usd": Decimal("0"),
         "caching_usd": None, "prefix_changed_requests": 0, "unverified_pricing": False},
    ]
    out = build_shadow(True, rows)
    assert out["enabled"] and out["estimate"] and out["outputs_compared"] is False
    assert (out["compression_usd"], out["caching_usd"]) == (0.12, 1.5)
    assert out["steps"][1]["caching_usd"] == 0.0
    assert (out["steps"][0]["requests"], out["steps"][0]["sampled_requests"]) == (120, 12)
    assert out["unverified_pricing"] and not out["steps"][1]["unverified_pricing"]


def test_shadow_endpoints(monkeypatch):
    calls = []

    async def fake_fetchrow(query: str, *args):
        calls.append((query, args))
        return {"enabled": True, "id": WORKSPACE}

    async def fake_fetch(query: str, *args):
        assert "shadow_saving_usd IS NOT NULL" in query and args == (WORKSPACE, 7)
        return []

    monkeypatch.setattr(dbmod.db, "fetchrow", fake_fetchrow)
    monkeypatch.setattr(dbmod.db, "fetch", fake_fetch)
    app.dependency_overrides[current_workspace] = lambda: WORKSPACE
    try:
        client = TestClient(app)
        assert client.get("/api/v1/optimize/shadow").json()["enabled"] is True
        assert client.post("/api/v1/optimize/shadow", json={"enabled": False}).json() == {"enabled": False}
        assert client.post("/api/v1/optimize/shadow", json={"enabled": "maybe"}).status_code == 422
    finally:
        app.dependency_overrides.clear()
    update_query, update_args = calls[-1]
    assert "UPDATE workspaces SET compression_shadow" in update_query and update_args == (WORKSPACE, False)
