#!/usr/bin/env python3
"""Seed a demo workspace with realistic historical usage for an investor pitch.

Built for hussain@tokenixindex.com / "Tokenix Demo", but nothing here is
hardcoded to that identity beyond the CLI defaults — every field is a flag.

What this does NOT do, on purpose:
  - It does not create the Clerk account. Clerk has no server-side "create a
    user with this email" call that belongs in a seed script; create
    hussain@tokenixindex.com in the Clerk dashboard (or have them sign up),
    then pass --clerk-user-id here to link it. Without --clerk-user-id the
    workspace is created unlinked and /connect's "Already have a key?" flow
    (lib/workspace.ts:linkExistingKey) links it after the fact using the
    minted txk- key this script prints.
  - It does not fabricate provider API keys. --openai-key/--anthropic-key/
    --google-key (or DEMO_OPENAI_API_KEY / DEMO_ANTHROPIC_API_KEY /
    DEMO_GOOGLE_API_KEY) must be real, working keys for that provider's
    traffic to work during a live demo request (scripts/demo_request.py).
    A provider with no key supplied is skipped, not stubbed — a fake
    "real-looking" key would silently break the one live moment of the pitch.
  - It does not write to sql/supabase/*.sql or add a new migration. Schema
    migrations are reviewed, idempotent, and (for the TimescaleDB ones)
    auto-applied on every analytics-api startup — see migrations.py. Demo
    data is neither: it's environment-specific and shouldn't run on every
    deploy. This script is the only place it lives, invoked by hand.

Two databases, matching the split documented in
sql/supabase/001_agent_sessions.sql:
  - TimescaleDB (--database-url / $DATABASE_URL): workspaces, api_keys,
    provider_keys, budgets, usage_records — the real per-request ledger.
  - Supabase (--supabase-url/--supabase-key or $SUPABASE_URL/
    $SUPABASE_SERVICE_KEY): agent_sessions, the rollup the sessions pages
    read. Skipped with a warning if not configured — usage_records seeding
    still succeeds, so /dashboard/logs and /dashboard/insights work even
    without Supabase creds; only /dashboard/sessions needs them.

Cost model is a simplified stand-in for the real ACPI engine
(ai-gateway's tools/ and tokenix/scripts/acpi.py) — approximate per-model
list prices, not the live index. Good enough for a demo narrative, not a
pricing source of truth.

Usage:
    pip install -r scripts/requirements-demo.txt
    export DATABASE_URL=postgresql://tokenix:tokenix@localhost:5432/tokenix
    export ENCRYPTION_KEY=...          # required only if seeding provider_keys
    export SUPABASE_URL=...            # optional, for agent_sessions
    export SUPABASE_SERVICE_KEY=...    # optional, for agent_sessions
    export DEMO_OPENAI_API_KEY=sk-...  # optional, real key, BYOK for the demo
    python scripts/seed_demo_workspace.py --email hussain@tokenixindex.com

Refreshing an existing demo workspace (keeps its provider keys, budget and
any real requests; replaces only earlier demo rows, in one transaction):
    python scripts/seed_demo_workspace.py --workspace-id <uuid> \\
        --since 2026-10-01 --replace-seeded --seed 1031 --dry-run   # then without --dry-run

Re-running is safe: --reset deletes this workspace's previously seeded
usage_records / agent_sessions / provider_keys / budget before reseeding, so
iterating on the mock data shape doesn't pile up duplicate sessions.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import os
import random
import secrets
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg

log = logging.getLogger("seed_demo_workspace")

DEFAULT_DATABASE_URL = "postgresql://tokenix:tokenix@localhost:5432/tokenix"

# ── agents: what the demo workspace runs ─────────────────────────────────────
# Two agent loops on two providers, shaped like real gateway traffic so the
# Optimize page has something to analyze: a stable system prompt + tool
# definitions sent on every call, history that grows as tool results come
# back, a few upstream errors retried. Rows are priced by the gateway's own
# price_request against the shipped ACPI catalog, so every dollar figure
# reconciles with its tokens exactly as a real request's would.
_GATEWAY = Path(__file__).resolve().parents[1] / "apps" / "gateway"
sys.path.insert(0, str(_GATEWAY))
from src.services.acpi import AcpiCatalog  # noqa: E402
from src.services.cost import price_request  # noqa: E402


@dataclass(frozen=True)
class AgentStep:
    name: str
    model_id: str
    calls: tuple[int, int]  # how many times the step runs per session
    output: tuple[int, int]  # output tokens per call
    result: tuple[int, int]  # tool result tokens returned per call; (0, 0) = no tool call


@dataclass(frozen=True)
class Agent:
    feature_tag: str
    provider: str
    share: float  # of sessions
    system_tokens: int
    tool_tokens: int
    tool_count: int
    max_output_tokens: int | None
    # OpenAI caches a long stable prefix by itself; Anthropic only caches
    # what the request marks with cache_control, which this agent doesn't.
    auto_cache: bool
    steps: tuple[AgentStep, ...]


_AGENTS = (
    Agent("support-agent", "openai", 0.55, 1850, 4300, 24, 800, True, (
        AgentStep("triage", "gpt-4o", (1, 1), (30, 90), (0, 0)),
        AgentStep("lookup", "gpt-4o", (2, 5), (40, 140), (500, 1400)),
        AgentStep("reply", "gpt-4o", (1, 1), (160, 480), (0, 0)),
    )),
    Agent("research-agent", "anthropic", 0.45, 2600, 2700, 12, None, False, (
        AgentStep("plan", "claude-sonnet-4.5", (1, 1), (200, 600), (0, 0)),
        AgentStep("search", "claude-sonnet-4.5", (2, 6), (60, 220), (900, 2400)),
        AgentStep("synthesize", "claude-sonnet-4.5", (1, 1), (700, 2200), (0, 0)),
        AgentStep("check", "claude-haiku-4.5", (1, 1), (80, 260), (0, 0)),
    )),
)

#: Share of calls that hit an upstream 429/500 first and are retried.
_RETRY_RATE = 0.04
#: OpenAI's automatic prefix cache misses now and then (eviction, a new replica).
_AUTO_CACHE_HIT = 0.85
#: The support agent's system prompt was revised on this day: a second prefix.
_PROMPT_REVISED = datetime(2026, 10, 20, tzinfo=timezone.utc)

_OUTCOME_TAGS = ["ticket_resolved", "lead_qualified", "doc_generated", "task_completed"]

# Hour-of-day weights: business hours dominate, nights are thin. Index 0 = 00:00 UTC.
_HOUR_WEIGHTS = [
    1, 1, 1, 1, 1, 1, 2, 4, 7, 9, 10, 10,
    9, 10, 10, 9, 8, 6, 4, 3, 2, 2, 1, 1,
]


@dataclass
class Step:
    provider: str
    model_id: str
    step_name: str
    timestamp: datetime
    status_code: int
    latency_ms: int
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    # None on a failed call: the provider billed nothing, so there is no anatomy.
    system_tokens: int | None = None
    tool_tokens: int | None = None
    history_tokens: int | None = None
    user_tokens: int | None = None
    tool_count: int | None = None
    max_output_tokens: int | None = None
    prefix_hash: str | None = None
    priced: bool = True
    cost_usd: float = 0.0
    acpi_bench_usd: float = 0.0
    uncached_input_cost_usd: float | None = None
    cache_read_cost_usd: float | None = None
    cache_write_cost_usd: float | None = None
    output_cost_usd: float | None = None
    prefix_cache_saving_usd: float | None = None

    @property
    def uncached_input_tokens(self) -> int | None:
        return None if self.system_tokens is None else self.input_tokens - self.cached_input_tokens


@dataclass
class Session:
    session_id: str
    workspace_id: UUID
    feature_tag: str
    status: str
    steps: list[Step] = field(default_factory=list)
    outcome: str | None = None
    outcome_value: float | None = None

    @property
    def started_at(self) -> datetime:
        return self.steps[0].timestamp

    @property
    def completed_at(self) -> datetime:
        return self.steps[-1].timestamp

    @property
    def total_cost_usd(self) -> float:
        return round(sum(s.cost_usd for s in self.steps), 8)

    @property
    def total_tokens(self) -> int:
        return sum(s.input_tokens + s.output_tokens for s in self.steps)

    @property
    def models_used(self) -> list[str]:
        seen: list[str] = []
        for s in self.steps:
            if s.model_id not in seen:
                seen.append(s.model_id)
        return seen


def load_catalog() -> AcpiCatalog:
    catalog = AcpiCatalog(_GATEWAY / "data" / "acpi_prices.json", refresh_seconds=0)
    catalog.load()
    return catalog


def _random_start(rng: random.Random, since: datetime, now: datetime) -> datetime:
    """A business-hours-weighted start between `since` and an hour ago, so a
    whole session (a few minutes) always ends in the past."""
    latest = now - timedelta(hours=1)
    days = (latest.date() - since.date()).days
    while True:
        day = since + timedelta(days=rng.randint(0, days))
        hour = rng.choices(range(24), weights=_HOUR_WEIGHTS, k=1)[0]
        ts = day.replace(hour=hour, minute=rng.randint(0, 59), second=rng.randint(0, 59), microsecond=0)
        if since <= ts <= latest:
            return ts


def _prefix_hash(agent: Agent, ts: datetime) -> str:
    revision = 2 if agent.feature_tag == "support-agent" and ts >= _PROMPT_REVISED else 1
    return hashlib.sha256(f"demo:{agent.feature_tag}:prompt-v{revision}".encode()).hexdigest()


def _build_session(
    rng: random.Random, catalog: AcpiCatalog, workspace_id: UUID, agent: Agent, start: datetime
) -> Session:
    session = Session(
        session_id=f"demo-{uuid4().hex[:16]}",
        workspace_id=workspace_id,
        feature_tag=agent.feature_tag,
        status="completed",
    )
    prefix = agent.system_tokens + agent.tool_tokens
    history = 0
    latest = rng.randint(60, 320)  # the user's opening message
    ts = start

    def call(step: AgentStep) -> Step:
        nonlocal ts
        if rng.random() < _RETRY_RATE:
            session.steps.append(Step(
                agent.provider, step.model_id, step.name, ts,
                status_code=rng.choice((429, 429, 500)), latency_ms=rng.randint(150, 1500),
            ))
            ts += timedelta(seconds=rng.randint(2, 20))
        output = rng.randint(*step.output)
        input_tokens = prefix + history + latest
        warm = any(s.status_code == 200 for s in session.steps)
        cached = 0
        if agent.auto_cache and warm and prefix >= 1024 and rng.random() < _AUTO_CACHE_HIT:
            cached = prefix // 128 * 128
        priced = price_request(
            catalog, agent.provider, step.model_id, input_tokens, output,
            cache_read_tokens=cached,
            uncached_prefix_tokens=max(prefix - cached, 0),
        )
        latency = int(300 + output * rng.uniform(8, 20))
        row = Step(
            agent.provider, step.model_id, step.name, ts, status_code=200, latency_ms=latency,
            input_tokens=input_tokens, output_tokens=output, cached_input_tokens=cached,
            system_tokens=agent.system_tokens, tool_tokens=agent.tool_tokens,
            history_tokens=history, user_tokens=latest,
            tool_count=agent.tool_count, max_output_tokens=agent.max_output_tokens,
            prefix_hash=_prefix_hash(agent, start), priced=priced.priced,
            cost_usd=round(priced.cost_usd, 8), acpi_bench_usd=round(priced.acpi_bench_usd, 8),
            uncached_input_cost_usd=round(priced.uncached_input_cost_usd, 8),
            cache_read_cost_usd=round(priced.cache_read_cost_usd, 8),
            cache_write_cost_usd=round(priced.cache_write_cost_usd, 8),
            output_cost_usd=round(priced.output_cost_usd, 8),
            prefix_cache_saving_usd=round(priced.prefix_cache_saving_usd, 8),
        )
        session.steps.append(row)
        ts += timedelta(milliseconds=latency, seconds=rng.randint(1, 15))
        return row

    for step in agent.steps:
        for _ in range(rng.randint(*step.calls)):
            row = call(step)
            # This turn's message and the model's reply join the history; a
            # tool result becomes the next latest message.
            history += latest + row.output_tokens
            latest = rng.randint(*step.result) if step.result[1] else rng.randint(40, 160)

    roll = rng.random()
    if roll < 0.03:
        session.status = "failed"
        last = session.steps[-1]
        session.steps[-1] = Step(last.provider, last.model_id, last.step_name, last.timestamp, status_code=500, latency_ms=rng.randint(150, 1500))
    elif roll < 0.06:
        session.status = "abandoned"
    elif rng.random() < 0.30:
        session.outcome = rng.choice(_OUTCOME_TAGS)
        session.outcome_value = round(rng.uniform(1, 50), 2)
    return session


def build_sessions(
    rng: random.Random, catalog: AcpiCatalog, workspace_id: UUID, count: int, since: datetime, now: datetime
) -> list[Session]:
    """`count` sessions split across the agents by share, started between
    `since` and an hour ago."""
    sessions = []
    for i, agent in enumerate(_AGENTS):
        n = count - len(sessions) if i == len(_AGENTS) - 1 else round(count * agent.share)
        sessions += [_build_session(rng, catalog, workspace_id, agent, _random_start(rng, since, now)) for _ in range(n)]
    return sessions


def _generate_key() -> str:
    return "txk-" + secrets.token_urlsafe(32)


def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def _resolve_workspace(
    conn: asyncpg.Connection, name: str, email: str, clerk_user_id: str | None
) -> tuple[UUID, bool]:
    """Find an existing demo workspace by email, or create one. Returns
    (workspace_id, created)."""
    existing = await conn.fetchrow("SELECT id FROM workspaces WHERE email = $1", email)
    if existing is not None:
        if clerk_user_id:
            await conn.execute(
                "UPDATE workspaces SET clerk_user_id = $1 WHERE id = $2",
                clerk_user_id,
                existing["id"],
            )
        return existing["id"], False

    workspace_id = await conn.fetchval(
        "INSERT INTO workspaces (name, email, clerk_user_id) VALUES ($1, $2, $3) RETURNING id",
        name,
        email,
        clerk_user_id,
    )
    return workspace_id, True


async def _mint_key(conn: asyncpg.Connection, workspace_id: UUID) -> str:
    raw_key = _generate_key()
    await conn.execute(
        "INSERT INTO api_keys (workspace_id, key_hash, key_prefix) VALUES ($1, $2, $3)",
        workspace_id,
        _hash_key(raw_key),
        raw_key[:12],
    )
    return raw_key


async def _seed_provider_keys(
    conn: asyncpg.Connection, workspace_id: UUID, keys: dict[str, str], encryption_key: str | None
) -> list[str]:
    if not keys:
        return []
    if not encryption_key:
        log.warning("provider keys supplied but ENCRYPTION_KEY is not set — skipping provider_keys")
        return []

    from cryptography.fernet import Fernet

    fernet = Fernet(encryption_key.encode() if isinstance(encryption_key, str) else encryption_key)
    seeded: list[str] = []
    for provider, plaintext in keys.items():
        encrypted = fernet.encrypt(plaintext.encode()).decode()
        await conn.execute(
            """
            INSERT INTO provider_keys (workspace_id, provider, encrypted_key)
            VALUES ($1, $2, $3)
            ON CONFLICT (workspace_id, provider) DO UPDATE SET encrypted_key = EXCLUDED.encrypted_key
            """,
            workspace_id,
            provider,
            encrypted,
        )
        seeded.append(provider)
    return seeded


async def _seed_budget(conn: asyncpg.Connection, workspace_id: UUID, email: str) -> None:
    await conn.execute(
        """
        INSERT INTO budgets (workspace_id, monthly_limit_usd, alert_pct, alert_email)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (workspace_id) DO NOTHING
        """,
        workspace_id,
        500.00,
        80,
        email,
    )


async def _reset_demo_data(conn: asyncpg.Connection, workspace_id: UUID) -> None:
    log.info("reset: deleting previously seeded usage_records/provider_keys/budget for %s", workspace_id)
    await conn.execute(
        "DELETE FROM usage_records WHERE workspace_id = $1 AND session_id LIKE 'demo-%'", workspace_id
    )
    await conn.execute("DELETE FROM provider_keys WHERE workspace_id = $1", workspace_id)
    await conn.execute("DELETE FROM budgets WHERE workspace_id = $1", workspace_id)


async def _delete_seeded(conn: asyncpg.Connection, workspace_id: UUID) -> int:
    """Only rows this script wrote (session_id 'demo-…'). Real requests,
    provider keys and the budget stay as they are."""
    status = await conn.execute(
        "DELETE FROM usage_records WHERE workspace_id = $1 AND session_id LIKE 'demo-%'", workspace_id
    )
    return int(status.split()[-1])


_COLUMNS = (
    "request_id", '"timestamp"', "workspace_id", "provider", "model_id", "feature_tag",
    "input_tokens", "output_tokens", "cached_input_tokens", "cost_usd", "acpi_bench_usd", "overpay_usd",
    "latency_ms", "status_code", "priced",
    "session_id", "step_name", "step_sequence", "session_name", "session_status",
    "system_tokens", "tool_tokens", "history_tokens", "user_tokens", "tool_count",
    "cache_write_tokens", "uncached_input_tokens", "max_output_tokens", "prefix_hash",
    "uncached_input_cost_usd", "cache_write_cost_usd", "cache_read_cost_usd", "output_cost_usd",
    "prefix_cache_saving_usd",
)


def usage_rows(sessions: list[Session]) -> list[tuple]:
    rows = []
    for session in sessions:
        for sequence, step in enumerate(session.steps, start=1):
            analyzed = step.system_tokens is not None
            rows.append((
                uuid4(), step.timestamp, session.workspace_id, step.provider, step.model_id, session.feature_tag,
                step.input_tokens, step.output_tokens, step.cached_input_tokens,
                step.cost_usd, step.acpi_bench_usd, round(step.cost_usd - step.acpi_bench_usd, 8),
                step.latency_ms, step.status_code, step.priced,
                session.session_id, step.step_name, sequence,
                session.session_id,  # session_name reuses the id — fine for demo
                session.status if session.status != "completed" else None,
                step.system_tokens, step.tool_tokens, step.history_tokens, step.user_tokens, step.tool_count,
                0 if analyzed else None, step.uncached_input_tokens, step.max_output_tokens, step.prefix_hash,
                step.uncached_input_cost_usd, step.cache_write_cost_usd, step.cache_read_cost_usd,
                step.output_cost_usd, step.prefix_cache_saving_usd,
            ))
    return rows


async def _insert_usage_records(conn: asyncpg.Connection, sessions: list[Session]) -> int:
    rows = usage_rows(sessions)
    placeholders = ", ".join(f"${i}" for i in range(1, len(_COLUMNS) + 1))
    await conn.executemany(
        f"INSERT INTO usage_records ({', '.join(_COLUMNS)}) VALUES ({placeholders})", rows
    )
    return len(rows)


async def _seed_agent_sessions(
    sessions: list[Session],
    workspace_id: UUID,
    supabase_url: str | None,
    supabase_key: str | None,
    replace: bool,
) -> int:
    if not supabase_url or not supabase_key:
        log.warning(
            "SUPABASE_URL/SUPABASE_SERVICE_KEY not set — skipping agent_sessions. "
            "usage_records still seeded, so logs/insights work; /dashboard/sessions will be empty."
        )
        return 0

    from supabase import create_async_client

    client = await create_async_client(supabase_url, supabase_key)

    if replace:
        await client.table("agent_sessions").delete().eq(
            "workspace_id", str(workspace_id)
        ).like("session_id", "demo-%").execute()

    rows = []
    for s in sessions:
        rows.append(
            {
                "session_id": s.session_id,
                "workspace_id": str(s.workspace_id),
                "name": None,
                "status": s.status,
                "started_at": s.started_at.isoformat(),
                "completed_at": s.completed_at.isoformat() if s.status != "running" else None,
                "last_activity_at": s.completed_at.isoformat(),
                "total_cost_usd": s.total_cost_usd,
                "total_tokens": s.total_tokens,
                "total_requests": len(s.steps),
                "models_used": s.models_used,
                "feature_tag": s.feature_tag,
                "outcome": s.outcome,
                "outcome_value": s.outcome_value,
                "cost_per_outcome": round(s.total_cost_usd / s.outcome_value, 6)
                if s.outcome_value
                else None,
            }
        )

    # Batch upsert, matching session_sync.py's own batching (it upserts per
    # cycle, not one row at a time) — chunk to stay well under any request
    # size limit.
    chunk_size = 100
    for i in range(0, len(rows), chunk_size):
        await client.table("agent_sessions").upsert(
            rows[i : i + chunk_size], on_conflict="session_id"
        ).execute()

    return len(rows)


def _summary(sessions: list[Session]) -> None:
    """Counts the dashboard pages will show; nothing secret."""
    steps = [step for s in sessions for step in s.steps]
    groups: dict[tuple[str, str, str], int] = {}
    for s in sessions:
        for step in s.steps:
            if step.system_tokens is not None:
                key = (s.feature_tag, step.step_name, step.model_id)
                groups[key] = groups.get(key, 0) + 1
    print(f"sessions:          {len(sessions)}")
    print(f"usage_records:     {len(steps)} ({sum(st.status_code != 200 for st in steps)} errors, "
          f"{sum(st.system_tokens is not None for st in steps)} analyzed)")
    print(f"first / last:      {min(st.timestamp for st in steps):%Y-%m-%d %H:%M} / {max(st.timestamp for st in steps):%Y-%m-%d %H:%M} UTC")
    print(f"total spend:       ${sum(st.cost_usd for st in steps):.2f}")
    print("analyzed requests per Optimize group (100+ gets recommendations):")
    for (feature, step_name, model), n in sorted(groups.items()):
        print(f"  {feature:16} {step_name:11} {model:18} {n}")


async def run(args: argparse.Namespace) -> None:
    rng = random.Random(args.seed)
    catalog = load_catalog()
    now = datetime.now(timezone.utc)
    since = (
        datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
        if args.since
        else now - timedelta(days=args.days)
    )

    if args.dry_run:
        _summary(build_sessions(rng, catalog, args.workspace_id or uuid4(), args.sessions, since, now))
        print("\nDry run: nothing written.")
        return

    provider_keys: dict[str, str] = {}
    for provider, cli_val, env_var in (
        ("openai", args.openai_key, "DEMO_OPENAI_API_KEY"),
        ("anthropic", args.anthropic_key, "DEMO_ANTHROPIC_API_KEY"),
        ("google", args.google_key, "DEMO_GOOGLE_API_KEY"),
    ):
        value = cli_val or os.getenv(env_var)
        if value:
            provider_keys[provider] = value
        else:
            log.warning("no %s key supplied (--%s-key or $%s) — BYOK for %s will be skipped", provider, provider, env_var, provider)

    conn = await asyncpg.connect(args.database_url)
    try:
        async with conn.transaction():
            if args.workspace_id:
                if not await conn.fetchval("SELECT 1 FROM workspaces WHERE id = $1", args.workspace_id):
                    raise SystemExit(f"no workspace {args.workspace_id}; nothing written")
                workspace_id, created = args.workspace_id, False
            else:
                workspace_id, created = await _resolve_workspace(
                    conn, args.workspace_name, args.email, args.clerk_user_id
                )
            log.info("workspace %s (%s)", workspace_id, "created" if created else "reused existing")

            deleted = 0
            if args.reset:
                await _reset_demo_data(conn, workspace_id)
            elif args.replace_seeded:
                deleted = await _delete_seeded(conn, workspace_id)
                log.info("replace-seeded: deleted %d earlier demo rows", deleted)

            raw_key: str | None = None
            if created or args.mint_key:
                raw_key = await _mint_key(conn, workspace_id)

            seeded_providers = await _seed_provider_keys(conn, workspace_id, provider_keys, args.encryption_key)
            await _seed_budget(conn, workspace_id, args.email)

            sessions = build_sessions(rng, catalog, workspace_id, args.sessions, since, now)
            await _insert_usage_records(conn, sessions)
    finally:
        await conn.close()

    supabase_count = await _seed_agent_sessions(
        sessions,
        workspace_id,
        args.supabase_url or os.getenv("SUPABASE_URL"),
        args.supabase_key or os.getenv("SUPABASE_SERVICE_KEY"),
        args.reset or args.replace_seeded,
    )

    print("\n--- demo workspace seeded ---")
    print(f"workspace_id:      {workspace_id}")
    if raw_key:
        print(f"api_key (txk-):    {raw_key}   <- store now, shown once")
    print(f"provider_keys set: {seeded_providers or '(none supplied; existing keys untouched)'}")
    print(f"demo rows deleted: {deleted}")
    _summary(sessions)
    print(f"agent_sessions:    {supabase_count} ({'Supabase' if supabase_count else 'SKIPPED — see warning above'})")
    print("\nNext steps:")
    print("  1. Create/confirm the Clerk account for this email, then re-run with --clerk-user-id to link it")
    print("     (or use /dashboard/connect's 'Already have a key?' flow with the txk- key above).")
    print("  2. Confirm dashboard pages load: /dashboard, /dashboard/logs, /dashboard/sessions, /dashboard/budgets.")
    print("  3. If BYOK providers were skipped above, live requests via demo_request.py will fail for those.")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--database-url", default=os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL))
    p.add_argument("--email", default="hussain@tokenixindex.com")
    p.add_argument("--workspace-name", default="Tokenix Demo")
    p.add_argument("--clerk-user-id", default=None, help="Link to this Clerk user id (from the Clerk dashboard).")
    p.add_argument("--mint-key", action="store_true", help="Mint an additional key even if the workspace already exists.")
    p.add_argument("--workspace-id", type=UUID, default=None, help="Seed this existing workspace (skips the lookup by email).")
    p.add_argument("--days", type=int, default=90, help="Spread sessions across this many trailing days.")
    p.add_argument("--since", default=None, help="Spread sessions from this date (YYYY-MM-DD, UTC) to now; overrides --days.")
    p.add_argument("--replace-seeded", action="store_true",
                   help="Delete only this workspace's earlier demo rows (session_id 'demo-…') first. "
                        "Unlike --reset, keeps provider keys and the budget.")
    p.add_argument("--dry-run", action="store_true", help="Print what would be seeded; don't connect to any database.")
    p.add_argument("--sessions", type=int, default=75, help="Number of sessions to generate (50-100 is the demo target).")
    p.add_argument("--seed", type=int, default=None, help="Random seed, for reproducible demo data.")
    p.add_argument("--reset", action="store_true", help="Delete this workspace's previously seeded demo data first.")
    p.add_argument("--encryption-key", default=os.getenv("ENCRYPTION_KEY"))
    p.add_argument("--openai-key", default=None, help="Real OpenAI key for BYOK (or $DEMO_OPENAI_API_KEY).")
    p.add_argument("--anthropic-key", default=None, help="Real Anthropic key for BYOK (or $DEMO_ANTHROPIC_API_KEY).")
    p.add_argument("--google-key", default=None, help="Real Google key for BYOK (or $DEMO_GOOGLE_API_KEY).")
    p.add_argument("--supabase-url", default=None)
    p.add_argument("--supabase-key", default=None, help="Supabase service-role key (or $SUPABASE_SERVICE_KEY).")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main() -> None:
    args = parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")
    try:
        asyncio.run(run(args))
    except (asyncpg.PostgresError, OSError) as exc:
        log.error("could not seed demo workspace: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
