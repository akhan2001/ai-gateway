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
from uuid import UUID, uuid4

import asyncpg

log = logging.getLogger("seed_demo_workspace")

DEFAULT_DATABASE_URL = "postgresql://tokenix:tokenix@localhost:5432/tokenix"

# ── model catalog: rough list prices, $/1M tokens (input, output) ───────────
# Not the live ACPI catalog — see module docstring.
_MODELS: dict[str, list[tuple[str, float, float]]] = {
    "openai": [
        ("gpt-4o", 2.50, 10.00),
        ("gpt-4-turbo", 10.00, 30.00),
        ("o1", 15.00, 60.00),
    ],
    "anthropic": [
        ("claude-3-5-sonnet-20241022", 3.00, 15.00),
        ("claude-3-opus-20240229", 15.00, 75.00),
    ],
    "google": [
        ("gemini-2.0-flash", 0.10, 0.40),
        ("gemini-1.5-pro", 1.25, 5.00),
    ],
}
_PROVIDER_WEIGHTS = {"openai": 0.40, "anthropic": 0.35, "google": 0.25}

_FEATURE_TAGS = [
    "customer-support-bot",
    "content-generation",
    "code-review-agent",
    "data-extraction",
    "research-assistant",
    "sales-email-drafting",
]

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
    input_tokens: int
    output_tokens: int
    cost_usd: float
    acpi_bench_usd: float
    latency_ms: int
    status_code: int
    timestamp: datetime
    step_name: str


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


def _random_timestamp(rng: random.Random, days: int) -> datetime:
    now = datetime.now(timezone.utc)
    day_offset = rng.uniform(0, days)
    hour = rng.choices(range(24), weights=_HOUR_WEIGHTS, k=1)[0]
    minute = rng.randint(0, 59)
    ts = now - timedelta(days=day_offset)
    return ts.replace(hour=hour, minute=minute, second=rng.randint(0, 59), microsecond=0)


def _pick_model(rng: random.Random) -> tuple[str, str, float, float]:
    provider = rng.choices(list(_PROVIDER_WEIGHTS), weights=list(_PROVIDER_WEIGHTS.values()), k=1)[0]
    model_id, rate_in, rate_out = rng.choice(_MODELS[provider])
    return provider, model_id, rate_in, rate_out


def _build_step(
    rng: random.Random,
    provider: str,
    model_id: str,
    rate_in: float,
    rate_out: float,
    target_cost: float,
    timestamp: datetime,
    step_index: int,
) -> Step:
    # Solve token counts from a target dollar cost at a 2.5:1 input:output
    # ratio (typical chat shape), then clip to plausible bounds and recompute
    # actual cost from the clipped, integer token counts — cost_usd in the DB
    # is always the literal product of tokens * rate, never a number that
    # doesn't reconcile with the tokens sitting next to it.
    ratio = rng.uniform(1.5, 3.5)
    denom = (ratio * rate_in + rate_out) / 1_000_000
    output_tokens = int(target_cost / denom) if denom > 0 else 50
    # Caps sized for the priciest model (gpt-4o) to actually reach a ~$1.50
    # session at a plausible token count (tens of thousands, not millions) —
    # a large-document / big-codebase-read step, not an absurd one. Cheap
    # models (gemini-flash) hit these caps far below their dollar target and
    # just come out cheap, which is realistic, not a bug: showing that the
    # same task costs very different amounts on different models is the
    # actual ACPI story.
    output_tokens = max(20, min(output_tokens, 15000))
    input_tokens = max(50, min(int(output_tokens * ratio), 40000))

    cost_usd = round((input_tokens * rate_in + output_tokens * rate_out) / 1_000_000, 8)
    market_factor = rng.uniform(0.85, 1.15)
    acpi_bench_usd = round(cost_usd * market_factor, 8)

    failed = rng.random() < 0.03
    return Step(
        provider=provider,
        model_id=model_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
        acpi_bench_usd=acpi_bench_usd,
        latency_ms=rng.randint(300, 6000),
        status_code=500 if failed else 200,
        timestamp=timestamp,
        step_name=f"step-{step_index}",
    )


def _build_session(rng: random.Random, workspace_id: UUID, days: int) -> Session:
    provider, model_id, rate_in, rate_out = _pick_model(rng)
    num_steps = rng.choices([1, 2, 3, 4, 5, 6, 7, 8], weights=[30, 22, 16, 12, 8, 6, 4, 2], k=1)[0]

    # Session-level cost drawn to land the fleet-wide total in a demo-shaped
    # range ($45-120 across ~75 sessions); see module docstring — this is a
    # target, not a guarantee, since actual cost is reconstructed from
    # integer token counts per step afterwards.
    session_cost = min(1.50, max(0.001, rng.lognormvariate(0.1, 0.6)))
    per_step_cost = session_cost / num_steps

    start = _random_timestamp(rng, days)
    steps: list[Step] = []
    ts = start
    for i in range(num_steps):
        steps.append(_build_step(rng, provider, model_id, rate_in, rate_out, per_step_cost, ts, i + 1))
        ts = ts + timedelta(seconds=rng.randint(20, 240))

    failed_any = any(s.status_code >= 400 for s in steps)
    status_roll = rng.random()
    if failed_any and status_roll < 0.6:
        status = "failed"
    elif status_roll < 0.04:
        status = "abandoned"
    else:
        status = "completed"

    session = Session(
        session_id=f"demo-{uuid4().hex[:16]}",
        workspace_id=workspace_id,
        feature_tag=rng.choice(_FEATURE_TAGS),
        status=status,
        steps=steps,
    )

    if status == "completed" and rng.random() < 0.30:
        session.outcome = rng.choice(_OUTCOME_TAGS)
        session.outcome_value = round(rng.uniform(1, 50), 2)

    return session


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
        ON CONFLICT (workspace_id) DO UPDATE SET
            monthly_limit_usd = EXCLUDED.monthly_limit_usd,
            alert_pct = EXCLUDED.alert_pct,
            alert_email = EXCLUDED.alert_email,
            updated_at = NOW()
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


async def _insert_usage_records(conn: asyncpg.Connection, sessions: list[Session]) -> int:
    rows = []
    for session in sessions:
        for sequence, step in enumerate(session.steps, start=1):
            overpay = round(step.cost_usd - step.acpi_bench_usd, 8)
            rows.append(
                (
                    uuid4(),
                    step.timestamp,
                    session.workspace_id,
                    step.provider,
                    step.model_id,
                    session.feature_tag,
                    step.input_tokens,
                    step.output_tokens,
                    step.cost_usd,
                    step.acpi_bench_usd,
                    overpay,
                    step.latency_ms,
                    step.status_code,
                    True,  # priced
                    session.session_id,
                    step.step_name,
                    sequence,
                    session.session_id,  # session_name reuses the id — fine for demo
                    session.status if session.status != "completed" else None,
                )
            )

    await conn.executemany(
        """
        INSERT INTO usage_records (
            request_id, "timestamp", workspace_id, provider, model_id, feature_tag,
            input_tokens, output_tokens, cost_usd, acpi_bench_usd, overpay_usd,
            latency_ms, status_code, priced,
            session_id, step_name, step_sequence, session_name, session_status
        ) VALUES (
            $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14,
            $15, $16, $17, $18, $19
        )
        """,
        rows,
    )
    return len(rows)


async def _seed_agent_sessions(
    sessions: list[Session],
    workspace_id: UUID,
    supabase_url: str | None,
    supabase_key: str | None,
    reset: bool,
) -> int:
    if not supabase_url or not supabase_key:
        log.warning(
            "SUPABASE_URL/SUPABASE_SERVICE_KEY not set — skipping agent_sessions. "
            "usage_records still seeded, so logs/insights work; /dashboard/sessions will be empty."
        )
        return 0

    from supabase import create_async_client

    client = await create_async_client(supabase_url, supabase_key)

    if reset:
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


async def run(args: argparse.Namespace) -> None:
    rng = random.Random(args.seed)

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
            workspace_id, created = await _resolve_workspace(
                conn, args.workspace_name, args.email, args.clerk_user_id
            )
            log.info("workspace %s (%s)", workspace_id, "created" if created else "reused existing")

            if args.reset:
                await _reset_demo_data(conn, workspace_id)

            raw_key: str | None = None
            if created or args.mint_key:
                raw_key = await _mint_key(conn, workspace_id)

            seeded_providers = await _seed_provider_keys(conn, workspace_id, provider_keys, args.encryption_key)
            await _seed_budget(conn, workspace_id, args.email)

            sessions = [_build_session(rng, workspace_id, args.days) for _ in range(args.sessions)]
            record_count = await _insert_usage_records(conn, sessions)
    finally:
        await conn.close()

    supabase_count = await _seed_agent_sessions(
        sessions,
        workspace_id,
        args.supabase_url or os.getenv("SUPABASE_URL"),
        args.supabase_key or os.getenv("SUPABASE_SERVICE_KEY"),
        args.reset,
    )

    total_cost = round(sum(s.total_cost_usd for s in sessions), 2)
    by_provider: dict[str, float] = {}
    for s in sessions:
        for step in s.steps:
            by_provider[step.provider] = round(by_provider.get(step.provider, 0.0) + step.cost_usd, 2)

    print("\n--- demo workspace seeded ---")
    print(f"workspace_id:      {workspace_id}")
    print(f"email:             {args.email}")
    if raw_key:
        print(f"api_key (txk-):    {raw_key}   <- store now, shown once")
    print(f"provider_keys set: {seeded_providers or '(none — no real keys supplied)'}")
    print(f"sessions:          {len(sessions)}")
    print(f"usage_records:     {record_count}")
    print(f"agent_sessions:    {supabase_count} ({'Supabase' if supabase_count else 'SKIPPED — see warning above'})")
    print(f"total spend:       ${total_cost}")
    print(f"by provider:       {by_provider}")
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
    p.add_argument("--days", type=int, default=90, help="Spread sessions across this many trailing days.")
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
