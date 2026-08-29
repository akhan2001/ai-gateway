# Tokenix — Claude Code Skills & Project Context

## What Tokenix is
AI Compute Price Index ("Bloomberg for AI"). Two repos:
- `tokenix/` — public dashboard + authenticated app (Next.js 16, Vercel)
- `ai-gateway/` — Python gateway + analytics API (Railway)

## Repo Map

### tokenix/
- `dashboard/app/(app)/` — authenticated pages: insights, benchmark, forecast, connect
- `dashboard/app/` — public pages: homepage, screener, calculator, methodology
- `dashboard/components/` — shared components, hand-rolled SVG charts (no recharts)
- `dashboard/lib/` — tokenix-api.ts, workspace.ts, require-key.ts
- `dashboard/proxy.ts` — Clerk middleware (NOT middleware.ts — that convention is deprecated in Next 16)
- `scripts/acpi.py` — ACPI calculation engine, runs hourly via GitHub Actions
- `dashboard/data/` — acpi_latest.json, acpi_history.csv, prices.csv

### ai-gateway/
- `apps/gateway/` — FastAPI proxy (port 8080), deployed on Railway
- `apps/analytics-api/` — FastAPI analytics (port 8001), deployed on Railway
- `apps/gateway/src/providers/` — OpenAI, Anthropic, Google adapters
- `apps/gateway/src/services/acpi.py` — ACPI pricing lookup
- `apps/gateway/src/services/db.py` — TimescaleDB writer
- `sql/` — schema migrations (001_schema.sql, 002_clerk_identity.sql)

## Stack
- Frontend: Next.js 16.2.6, TypeScript, Tailwind, hand-rolled SVG charts
- Auth: Clerk (@clerk/nextjs) — development keys currently, needs production keys
- Gateway: FastAPI + httpx, async Python, streaming SSE passthrough
- Analytics API: FastAPI, reads TimescaleDB
- Database: TimescaleDB on Railway (PostgreSQL + time-series extension)
- Cache: Upstash Redis (planned, not yet implemented)
- Deploy: Vercel (dashboard), Railway (gateway + analytics + DB)
- CI: GitHub Actions — runs acpi.py hourly, commits updated data files

## Production URLs
- Public site: tokenixindex.com
- Gateway: cozy-patience-production-815c.up.railway.app
- Analytics API: ai-gateway-production-a74e.up.railway.app
- TimescaleDB public: thomas.proxy.rlwy.net:38936

## Auth Architecture
- Dashboard login: Clerk (email/password) → workspace auto-provisioned on first /connect visit
- Gateway auth: txk- API key in Authorization header (machine to machine)
- Existing workspaces can be linked to Clerk accounts via /connect "Already have a key?" flow
- httpOnly cookie `tokenix_key` — REMOVED, replaced by Clerk session
- INTERNAL_API_TOKEN — shared secret between dashboard and analytics API for workspace provisioning

## Database Schema
Key tables in TimescaleDB:
- `workspaces` — id, name, clerk_user_id, email, created_at
- `api_keys` — id, workspace_id, key_hash (SHA-256), key_prefix (12 chars), created_at
- `provider_keys` — id, workspace_id, provider, encrypted_key (Fernet AES-256)
- `usage_records` — hypertable: workspace_id, timestamp, provider, model_id, input_tokens, output_tokens, cost_usd, acpi_bench_usd, overpay_usd, latency_ms, status_code

## ACPI Index
- Current value: ~$4.99/1M SCU
- 332 models, 56 providers
- Updates hourly via GitHub Actions → scripts/acpi.py
- Writes: dashboard/data/acpi_latest.json, acpi_history.csv, prices.csv
- Gateway loads acpi_prices.json at startup from apps/gateway/data/
- Methodology: 50/50 bucket weighted (premium + commodity), quality-adjusted

## Coding Conventions
- Python: async FastAPI, type hints, no print statements in production code use logging
- TypeScript: strict mode, no any, server components by default
- Charts: hand-rolled SVG only — recharts is NOT installed
- Middleware: proxy.ts not middleware.ts (Next 16 convention)
- Auth checks: resource-based in layout.tsx, NOT path matching in proxy.ts
- Never log: prompt content, completions, API keys (even partial), PII
- Never store API keys in plaintext — Fernet encryption for provider keys, SHA-256 for txk- keys

## Known Issues / Technical Debt
- Clerk is on development keys — needs production instance before real users
- ACPI prices baked into Docker image at deploy time — doesn't update hourly in production
- Sub-cent amounts round to $0.00 in dashboard — analytics API aggregates to 4dp server-side
- No txk- key rotation endpoint on gateway — customers who lose key have no self-serve recovery
- INTERNAL_API_TOKEN was exposed in chat transcript — rotate before production use

## Environment Variables

### Vercel (tokenix dashboard)
NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY
CLERK_SECRET_KEY
NEXT_PUBLIC_CLERK_SIGN_IN_URL=/sign-in
NEXT_PUBLIC_CLERK_SIGN_UP_URL=/sign-up
NEXT_PUBLIC_CLERK_AFTER_SIGN_IN_URL=/connect
NEXT_PUBLIC_CLERK_AFTER_SIGN_UP_URL=/connect
TOKENIX_ANALYTICS_URL=https://ai-gateway-production-a74e.up.railway.app
NEXT_PUBLIC_TOKENIX_GATEWAY_URL=https://cozy-patience-production-815c.up.railway.app
INTERNAL_API_TOKEN=(secret)
TOKENIX_ADMIN_TOKEN=local-dev-admin-token

### Railway — gateway service
DATABASE_URL=(timescale internal URL)
ENCRYPTION_KEY=(Fernet key)
ADMIN_TOKEN=(secret)

### Railway — analytics-api service
DATABASE_URL=(timescale internal URL)
INTERNAL_API_TOKEN=(secret, must match Vercel)

## What's Been Built
- Live ACPI index updating hourly
- Public screener with 332 models
- Token cost calculator (word-based + workflow-based)
- Methodology page
- Clerk authentication (sign-up, sign-in, sign-out)
- Workspace auto-provisioning on first /connect visit
- txk- key linking for existing workspaces
- Gateway proxy (OpenAI, Anthropic, Google adapters)
- Request logging to TimescaleDB
- ACPI benchmarking per request
- Analytics API (summary, usage, models, benchmark, forecast endpoints)
- Dashboard pages (insights, benchmark, forecast, connect)
- Authenticated layout with sidebar nav

## Next Up (Prioritised)
1. Fix Clerk production keys (dev keys have strict limits)
2. Rotate INTERNAL_API_TOKEN (exposed in transcript)
3. Company onboarding form (collect company_name, size, industry, spend tier, use case)
4. Export endpoints: CSV, Excel, PDF (analytics API)
5. Export buttons in dashboard UI
6. CLI tool: tokenix-cli npm package
7. Terminal dashboard mode (tokenix dashboard command)
8. Live ACPI refresh in production (fetch from URL not baked file)
9. txk- key rotation endpoint on gateway
10. Dashboard UI redesign (after Claude Design mockups ready)
11. Pricing page on tokenixindex.com
12. Stripe integration for paid tiers

## Business Context
- ICP: Series A-C AI-native SaaS companies
- Buyer: Head of AI, CTO, CFO
- Model: Free gateway (collect aggregated usage data) → sell ACPI data feed + dashboard tiers
- Gateway is free to use — revenue from data licensing and dashboard subscriptions
- Aggregated anonymous usage data feeds ACPI index (individual data never sold)
- Competitors: Helicone, Portkey, LangSmith (none have independent pricing benchmark)

---

# Appendix — carried over from the previous tokenix/CLAUDE.md

Preserved so the detail isn't lost when CLAUDE.md became a pointer. None of this
is duplicated above.

## Run / build
- Dev site: `npm run dev` (root) → runs `next dev` in `dashboard/`.
- Build: `npm run build` (root) installs dashboard deps then `next build`.
- Scrape prices: `python scripts/scrape_prices.py` → writes a new
  `data/snapshots/token_prices_<ts>.csv`.

## Python pipeline (`scripts/`)
- `scrape_prices.py` — multi-source token price scraper (`httpx` only).
- `acpi.py` — computes the ACPI index value + history.
- `benchmark_quality.py` — HF OpenEvals leaderboard → quality composite.
- `scheduler.py` — local hourly loop around `acpi.main()`.
- `data/snapshots/` — timestamped scraper output (`token_prices_*.csv`,
  `prices_*.csv`); gitignored working files that `dashboard/lib/data.ts` reads
  the newest of.

## Data contract (do not break)
The scraper and `dashboard/lib/data.ts` share a fixed CSV schema. Any new data
source or pipeline MUST emit these columns:
`timestamp, source, provider, model_id, model_name, context_length,
input_per_million_usd, output_per_million_usd`
`PriceRow` in `dashboard/lib/data.ts` mirrors it. `data.ts` prefers the newest
`../data/snapshots/token_prices_*.csv`, then falls back to
`dashboard/data/prices.csv`.

## Next.js version warning
Read `dashboard/AGENTS.md`: Next 16.2.6 has breaking changes vs. model training
data — check `node_modules/next/dist/docs/` before writing Next code. Tailwind
v4, shadcn, base-ui, lucide, cobe (globe) are in use. Most styling is **inline
styles**, not utility classes — match the surrounding file.

## Published ACPI methodology (the spec — kept in sync with the code)
- Unit: dollars per **1M Standard Compute Units** (all modalities normalized to
  cost per 1M tokens). Each model's per-token cost is blended **75% input / 25%
  output** (the standard 3:1 input:output usage assumption).
- ACPI = **two-bucket broad-market average** of every tracked model's
  risk-adjusted price. Models split into a **premium bucket** (Tier S/A) and a
  **commodity bucket** (Tier B/C); each bucket is equal-weighted internally, then
  the two are combined **50/50** so the cheaper long-tail half of the market pulls
  on the index as hard as the frontier (CPI-style), rather than flagships
  dominating. Tier assignment is a disclosed manual classification
  (`get_tier_weight` in `scripts/acpi.py`), reviewed monthly; the 50/50 split is
  `PREMIUM_BUCKET_WEIGHT` in `scripts/acpi.py`.
- Market-risk factor (P2) scales every price; the price-spread proxy that used to
  stand in for "quality" has been removed.
- Variant SKUs — batch-pricing, free-tier, and provider-alias listings
  (`:batch`, `:free`, `:extended`, `:nitro`, `:floor`, `~alias`) — are
  deduplicated to their base model before entering ACPI, so an OpenRouter catalog
  change doesn't get counted as new market entrants and inflate the index.
  `deduplicate_models`/`get_base_model_id` in `scripts/acpi.py`; the run's
  removed-variant count is reported as `deduped_variants_removed` in
  `acpi_latest.json`.
- Quality factor = **HELM-aligned benchmark composite** (MMLU-Pro, coding, math,
  reasoning), z-score normalised, sourced via standardized leaderboard
  aggregation (HF OpenEvals dataset — *not* raw Stanford HELM). It feeds the P1
  screener; models without benchmark data are excluded from P1 but kept in ACPI.
- Stated cadence: **updated daily** (hero copy says "One number, updated daily").
- Stated data sources in copy: provider docs (token pricing), Lambda Labs H100
  SXM5 median (GPU), Hyperstack vLLM / Llama 3.1 70B (throughput), HELM
  (benchmarks). Treat these as the target spec, not as already-wired inputs.

## What's real vs. placeholder in the index
The headline ACPI number is **real and implemented** — `scripts/acpi.py` fetches
live OpenRouter pricing, applies the adjustments above, and writes
`dashboard/data/acpi_latest.json` (read by `acpi-hero-card.tsx` via `loadAcpi()`)
plus an append-only `acpi_history.csv`. Still synthetic: the hero **spark /
17-month chart** (`buildSeries()` in `acpi-hero-card.tsx`, currently commented
out, and `acpi-chart.tsx`) — there is no real long history yet;
`acpi_history.csv` only accumulates from each run. See the `build-acpi-engine`
skill in `.claude/skills/`.

## Working norms
- Match existing style (inline styles, the serif/mono CSS vars, the "ledger" /
  terminal aesthetic). Don't introduce a CSS framework refactor unasked.
- Keep the index **auditable and append-only**: never overwrite historical CSV
  snapshots; add new timestamped rows/files.
- Don't fabricate index values in places that imply they're real. If wiring the
  number, replace the synthetic values; don't add more mock data beside them.
- Git user is `akhan2001`; remote is `github.com/akhan2001/tokenix`.
