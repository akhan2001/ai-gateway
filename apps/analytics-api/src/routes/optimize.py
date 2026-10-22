"""GET /api/v1/optimize/anatomy and /api/v1/optimize/recommendations.

Reads only the counts and per-part costs the gateway writes (sql/008). Prices
are never re-derived here: the gateway's services/cost.py is the one place
cache pricing lives, and every $ below is a sum of what it wrote.

Every recommendation is an estimate and says so, with the numbers behind it.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from ..auth import current_workspace
from ..db import db, f

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/optimize", tags=["optimize"])

#: Below this many analyzed requests a group's numbers are too noisy to act on.
MIN_REQUESTS = 100

# Thresholds for the rules. Kept here so the dashboard copy can quote them.
CACHE_MIN_PREFIX_TOKENS = 1024
CACHE_MIN_STABILITY = 0.8
CACHE_MAX_HIT_RATE = 0.5
TOOL_SHARE = 0.30
TOOLS_TYPICALLY_NEEDED = 5
HISTORY_SHARE = 0.50
HISTORY_MIN_TOKENS = 4000
HISTORY_KEPT_AFTER_SUMMARY = 0.25
OUTPUT_TAIL_RATIO = 4.0

_GROUPS_SQL = """
WITH r AS (
    SELECT COALESCE(feature_tag, '(untagged)') AS feature,
           COALESCE(step_name, '(none)')     AS step,
           provider, model_id, "timestamp",
           input_tokens, output_tokens, cached_input_tokens,
           cache_write_tokens, uncached_input_tokens,
           system_tokens, tool_tokens, history_tokens, user_tokens,
           tool_count, max_output_tokens, prefix_hash,
           uncached_input_cost_usd, cache_write_cost_usd, cache_read_cost_usd,
           output_cost_usd, prefix_cache_saving_usd
    FROM usage_records
    WHERE workspace_id = $1 AND priced AND system_tokens IS NOT NULL
      AND "timestamp" >= NOW() - make_interval(days => $2)
), p AS (
    SELECT feature, step, provider, model_id,
           percentile_cont(0.5)  WITHIN GROUP (ORDER BY output_tokens) AS out_p50,
           percentile_cont(0.95) WITHIN GROUP (ORDER BY output_tokens) AS out_p95
    FROM r GROUP BY 1, 2, 3, 4
)
SELECT r.feature, r.step, r.provider, r.model_id,
       COUNT(*)                                   AS requests,
       EXTRACT(EPOCH FROM MAX(r."timestamp") - MIN(r."timestamp")) / 86400.0 AS span_days,
       SUM(r.input_tokens)                        AS input_tokens,
       SUM(r.output_tokens)                       AS output_tokens,
       SUM(r.cached_input_tokens)                 AS cache_read_tokens,
       SUM(r.cache_write_tokens)                  AS cache_write_tokens,
       SUM(r.uncached_input_tokens)               AS uncached_input_tokens,
       SUM(r.system_tokens)                       AS system_tokens,
       SUM(r.tool_tokens)                         AS tool_tokens,
       SUM(r.history_tokens)                      AS history_tokens,
       SUM(r.user_tokens)                         AS user_tokens,
       AVG(r.tool_count)                          AS avg_tool_count,
       COUNT(DISTINCT r.prefix_hash)              AS distinct_prefixes,
       COUNT(*) FILTER (WHERE r.max_output_tokens IS NULL) AS no_output_limit,
       SUM(r.uncached_input_cost_usd)             AS uncached_input_usd,
       SUM(r.cache_write_cost_usd)                AS cache_write_usd,
       SUM(r.cache_read_cost_usd)                 AS cache_read_usd,
       SUM(r.output_cost_usd)                     AS output_usd,
       SUM(r.prefix_cache_saving_usd)             AS prefix_cache_saving_usd,
       MAX(p.out_p50)                             AS out_p50,
       MAX(p.out_p95)                             AS out_p95,
       SUM(GREATEST(r.output_tokens - p.out_p95, 0)) AS output_tail_tokens
FROM r JOIN p USING (feature, step, provider, model_id)
GROUP BY 1, 2, 3, 4
"""

_INT_FIELDS = (
    "requests", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
    "uncached_input_tokens", "system_tokens", "tool_tokens", "history_tokens", "user_tokens",
    "distinct_prefixes", "no_output_limit",
)
_USD_FIELDS = ("uncached_input_usd", "cache_write_usd", "cache_read_usd", "output_usd", "prefix_cache_saving_usd")
SECTIONS = ("system_tokens", "tool_tokens", "history_tokens", "user_tokens")


def _clean(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    for key in _INT_FIELDS:
        out[key] = int(row.get(key) or 0)
    for key in _USD_FIELDS + ("span_days", "avg_tool_count", "out_p50", "out_p95", "output_tail_tokens"):
        out[key] = f(row.get(key))
    return out


async def fetch_groups(workspace_id: UUID, days: int) -> list[dict[str, Any]]:
    return [_clean(row) for row in await db.fetch(_GROUPS_SQL, workspace_id, days)]


def _anatomy_of(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = lambda key: sum(row[key] for row in rows)  # noqa: E731
    input_tokens = total("input_tokens")
    return {
        "requests": total("requests"),
        "input_tokens": input_tokens,
        "output_tokens": total("output_tokens"),
        "sections": {key.removesuffix("_tokens"): total(key) for key in SECTIONS},
        "share": {key.removesuffix("_tokens"): (total(key) / input_tokens if input_tokens else 0.0) for key in SECTIONS},
        "cache_hit_rate": total("cache_read_tokens") / input_tokens if input_tokens else 0.0,
        "usd": {
            "uncached_input": round(total("uncached_input_usd"), 6),
            "cache_write": round(total("cache_write_usd"), 6),
            "cache_read": round(total("cache_read_usd"), 6),
            "output": round(total("output_usd"), 6),
        },
    }


def build_anatomy(groups: list[dict[str, Any]], days: int) -> dict[str, Any]:
    by_step: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in groups:
        by_step.setdefault((row["feature"], row["step"]), []).append(row)
    steps = [{"feature": feature, "step": step, **_anatomy_of(rows)} for (feature, step), rows in by_step.items()]
    steps.sort(key=lambda s: s["input_tokens"], reverse=True)
    overall = _anatomy_of(groups)
    return {
        "days": days,
        "analyzed_requests": overall["requests"],
        "min_requests": MIN_REQUESTS,
        "overall": overall,
        "steps": steps,
    }


def _monthly(group: dict[str, Any], usd_in_window: float) -> float:
    """Scale what the window saw to 30 days. The span is clamped to a day so a
    burst of requests in one afternoon doesn't extrapolate to a fortune."""
    return usd_in_window * 30.0 / max(group["span_days"], 1.0)


def _recommendations_for(g: dict[str, Any]) -> list[dict[str, Any]]:
    n = g["requests"]
    input_tokens = g["input_tokens"] or 1
    input_usd = g["uncached_input_usd"] + g["cache_write_usd"] + g["cache_read_usd"]
    usd_per_input_token = input_usd / input_tokens  # blended, after whatever caching already happens
    usd_per_output_token = g["output_usd"] / g["output_tokens"] if g["output_tokens"] else 0.0
    prefix_per_request = (g["system_tokens"] + g["tool_tokens"]) / n
    stability = 1.0 - (max(g["distinct_prefixes"], 1) - 1) / n
    hit_rate = g["cache_read_tokens"] / input_tokens
    recs: list[dict[str, Any]] = []

    def add(kind: str, title: str, usd_in_window: float, basis: dict[str, Any], note: str) -> None:
        if usd_in_window <= 0:
            return
        recs.append({
            "kind": kind,
            "title": title,
            "feature": g["feature"], "step": g["step"], "provider": g["provider"], "model": g["model_id"],
            "estimate": True,
            "monthly_usd": round(_monthly(g, usd_in_window), 2),
            "basis": {"requests": n, **basis},
            "note": note,
        })

    if prefix_per_request >= CACHE_MIN_PREFIX_TOKENS and stability >= CACHE_MIN_STABILITY and hit_rate < CACHE_MAX_HIT_RATE:
        how = (
            "Anthropic only caches with cache_control, which the Tokenix gateway does not send yet; this is what it would save once it does."
            if g["provider"] == "anthropic"
            else "This provider caches automatically; a low hit rate usually means something early in the prompt changes per request (a timestamp, an ID, reordered tools)."
        )
        add("enable_caching", "Cache the stable prefix", g["prefix_cache_saving_usd"] * stability, {
            "avg_prefix_tokens": round(prefix_per_request),
            "prefix_stability": round(stability, 3),
            "cache_hit_rate": round(hit_rate, 3),
        }, f"Estimate before one-time cache-write costs. {how}")

    tool_share = g["tool_tokens"] / input_tokens
    if tool_share > TOOL_SHARE and g["avg_tool_count"] > TOOLS_TYPICALLY_NEEDED:
        trimmed = 1 - TOOLS_TYPICALLY_NEEDED / g["avg_tool_count"]
        add("tool_search", "Use provider tool search / deferred tool loading", g["tool_tokens"] * trimmed * usd_per_input_token, {
            "tool_share": round(tool_share, 3),
            "avg_tool_count": round(g["avg_tool_count"], 1),
            "avg_tool_tokens": round(g["tool_tokens"] / n),
        }, f"Assumes a typical turn needs about {TOOLS_TYPICALLY_NEEDED} of its {g['avg_tool_count']:.0f} tools loaded.")

    history_share = g["history_tokens"] / input_tokens
    if history_share > HISTORY_SHARE and g["history_tokens"] / n >= HISTORY_MIN_TOKENS:
        add("summarize_history", "Summarize older turns (opt-in, later)", g["history_tokens"] * (1 - HISTORY_KEPT_AFTER_SUMMARY) * usd_per_input_token, {
            "history_share": round(history_share, 3),
            "avg_history_tokens": round(g["history_tokens"] / n),
        }, f"Assumes a summary keeps {HISTORY_KEPT_AFTER_SUMMARY:.0%} of the history. Changes what the model sees, so test quality first. Not available in Tokenix yet.")

    no_limit_share = g["no_output_limit"] / n
    if no_limit_share >= 0.5 and g["out_p50"] > 0 and g["out_p95"] >= OUTPUT_TAIL_RATIO * g["out_p50"]:
        add("output_limits", "Set output limits", g["output_tail_tokens"] * usd_per_output_token, {
            "requests_without_limit": round(no_limit_share, 3),
            "output_p50": round(g["out_p50"]),
            "output_p95": round(g["out_p95"]),
        }, "Saving if output were capped at today's p95 length. Check those longest answers didn't need it before capping.")

    return recs


def build_recommendations(groups: list[dict[str, Any]], days: int) -> dict[str, Any]:
    eligible = [g for g in groups if g["requests"] >= MIN_REQUESTS]
    recs = [rec for g in eligible for rec in _recommendations_for(g)]
    recs.sort(key=lambda r: r["monthly_usd"], reverse=True)
    return {
        "days": days,
        "analyzed_requests": sum(g["requests"] for g in groups),
        "min_requests": MIN_REQUESTS,
        "groups_skipped_for_low_volume": len(groups) - len(eligible),
        "recommendations": recs,
    }


@router.get("/anatomy")
async def anatomy(days: int = Query(30, ge=1, le=90), workspace_id: UUID = Depends(current_workspace)) -> dict:
    return build_anatomy(await fetch_groups(workspace_id, days), days)


@router.get("/recommendations")
async def recommendations(days: int = Query(30, ge=1, le=90), workspace_id: UUID = Depends(current_workspace)) -> dict:
    return build_recommendations(await fetch_groups(workspace_id, days), days)


# --- compression shadow mode (sql/009) -------------------------------------

SHADOW_DAYS = 7

# Read via to_jsonb so a database the gateway hasn't migrated yet reads "off".
_SHADOW_ENABLED_SQL = """
SELECT COALESCE((to_jsonb(w) ->> 'compression_shadow')::boolean, FALSE) AS enabled
FROM workspaces w WHERE w.id = $1
"""

# Both savings over the same requests: the ones shadow mode measured.
_SHADOW_SQL = """
SELECT COALESCE(feature_tag, '(untagged)') AS feature,
       COALESCE(step_name, '(none)')     AS step,
       COUNT(*)                          AS requests,
       SUM(input_tokens)                 AS input_tokens,
       SUM(shadow_saving_tokens)         AS saving_tokens,
       SUM(shadow_saving_usd)            AS compression_usd,
       SUM(prefix_cache_saving_usd)      AS caching_usd,
       COUNT(*) FILTER (WHERE shadow_prefix_changed) AS prefix_changed_requests
FROM usage_records
WHERE workspace_id = $1 AND shadow_saving_usd IS NOT NULL
  AND "timestamp" >= NOW() - make_interval(days => $2)
GROUP BY 1, 2
ORDER BY compression_usd DESC
"""


def build_shadow(enabled: bool, rows: list[dict[str, Any]]) -> dict[str, Any]:
    steps = [
        {
            "feature": row["feature"],
            "step": row["step"],
            "requests": int(row["requests"]),
            "input_tokens": int(row["input_tokens"] or 0),
            "saving_tokens": int(row["saving_tokens"] or 0),
            "compression_usd": round(f(row["compression_usd"]), 6),
            "caching_usd": round(f(row["caching_usd"]), 6),
            "prefix_changed_requests": int(row["prefix_changed_requests"]),
        }
        for row in rows
    ]
    return {
        "enabled": enabled,
        "days": SHADOW_DAYS,
        "estimate": True,
        "outputs_compared": False,
        "steps": steps,
        "compression_usd": round(sum(s["compression_usd"] for s in steps), 6),
        "caching_usd": round(sum(s["caching_usd"] for s in steps), 6),
    }


@router.get("/shadow")
async def shadow(workspace_id: UUID = Depends(current_workspace)) -> dict:
    row = await db.fetchrow(_SHADOW_ENABLED_SQL, workspace_id)
    try:
        rows = await db.fetch(_SHADOW_SQL, workspace_id, SHADOW_DAYS)
    except asyncpg.UndefinedColumnError:
        log.warning("shadow columns missing: the gateway has not applied sql/009 yet")
        rows = []
    return build_shadow(bool(row and row["enabled"]), rows)


class ShadowSetting(BaseModel):
    enabled: bool


@router.post("/shadow")
async def set_shadow(body: ShadowSetting, workspace_id: UUID = Depends(current_workspace)) -> dict:
    await db.fetchrow("UPDATE workspaces SET compression_shadow = $2 WHERE id = $1 RETURNING id", workspace_id, body.enabled)
    return {"enabled": body.enabled}
