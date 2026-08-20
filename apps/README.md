# Tokenix Gateway

An OpenAI-compatible proxy that prices every request against the **ACPI**
(AI Compute Price Index) as it passes through.

```
Customer changes one line
  -> all AI traffic flows through the gateway
  -> every request is logged (model, tokens, cost)
  -> benchmarked against the ACPI market rate
  -> dashboard shows spend vs market in real time
```

## Where this lives

This repo is a fork of the Helicone Rust gateway. **The Tokenix services are
pure Python and do not touch the Rust crate** — `ai-gateway/`, `crates/` and
`Cargo.*` are untouched on this branch. Everything Tokenix is under:

```
apps/gateway/         FastAPI + httpx proxy
apps/analytics-api/   FastAPI read API for the dashboard
sql/001_schema.sql    TimescaleDB schema (runs on first DB boot)
apps/gateway/data/     ACPI dataset, synced from the index pipeline
tools/                dataset exporter
docker-compose.yml    gateway + analytics + TimescaleDB
```

## Quick start

```bash
# 1. Generate the encryption key (protects stored provider credentials)
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

cat > .env <<'EOF'
ENCRYPTION_KEY=<paste it here>
ADMIN_TOKEN=local-dev-admin-token
EOF

# 2. Refresh the ACPI dataset from the index pipeline
python tools/export_acpi_prices.py --source ../tokenix/dashboard/data

# 3. Bring up the stack
docker compose up --build
```

Gateway on `:8080`, analytics on `:8001`, TimescaleDB on `:5432`.

## Provisioning a workspace

```bash
# Create a workspace; the txk- key is returned exactly once.
curl -s -X POST localhost:8080/admin/workspaces \
  -H "x-admin-token: local-dev-admin-token" \
  -H 'content-type: application/json' \
  -d '{"name":"acme"}'

# Store the customer's own provider credential (encrypted at rest).
curl -s -X POST localhost:8080/admin/workspaces/<workspace_id>/provider-keys \
  -H "x-admin-token: local-dev-admin-token" \
  -H 'content-type: application/json' \
  -d '{"provider":"openai","api_key":"sk-..."}'
```

## The one-line integration

```python
from openai import OpenAI

client = OpenAI(
    api_key="txk-your-tokenix-key",
    base_url="https://gateway.tokenixindex.com/openai/v1",
)
```

```typescript
import OpenAI from "openai";

const client = new OpenAI({
  apiKey: "txk-your-tokenix-key",
  baseURL: "https://gateway.tokenixindex.com/openai/v1",
});
```

Swap `/openai/` for `/anthropic/` or `/google/` and keep using the OpenAI SDK —
the adapters translate in both directions, including streaming.

Optional tagging, to slice spend by product surface:

```
Tokenix-Feature:  search
Tokenix-Workload: production
```

## Analytics endpoints

All take `Authorization: Bearer txk-...`. The workspace is resolved **from the
key**, never from a query parameter, so one customer cannot read another's
spend.

| Endpoint | What it returns |
| --- | --- |
| `GET /api/v1/summary` | The four dashboard cards: this month, MoM, vs ACPI, top model |
| `GET /api/v1/usage?days=&group_by=` | Time series or breakdown by `model`/`provider`/`feature`/`workload`/`day` |
| `GET /api/v1/models?days=` | Per-model spend, tokens and ACPI score |
| `GET /api/v1/benchmark?days=` | Actual vs market rate per model, plus savings opportunities |
| `GET /api/v1/forecast` | End-of-month and end-of-year projections |

## How pricing works

Two numbers, and conflating them would break the product:

- **`cost_usd`** — what the request actually cost at the model's list price.
- **`acpi_bench_usd`** — what the *same token volume* costs at the market-wide
  ACPI rate (the headline "$ per 1M Standard Compute Units", currently
  **$5.0284**).

Benchmarking a model against its own list price would make overpayment
identically zero on every request, so the **market rate** is the basis.
`overpay_usd = cost_usd - acpi_bench_usd`: positive means above market.

Token counts always come from what the provider reports — never estimated
locally — because that is what the customer is actually billed on.

Models absent from the ACPI dataset are recorded with `priced = false` rather
than a cost of zero, and every analytics query filters on `priced` so an
unknown model is never silently reported as free.

## Refreshing the ACPI dataset

```bash
# in the tokenix repo
python scripts/acpi.py

# here
python tools/export_acpi_prices.py --source ../tokenix/dashboard/data
```

It reads `prices.csv`, the full variant-deduplicated catalog — not
`prices_latest.csv`, which is the narrower calculator cut. A model missing from
this file is recorded `priced = false` and excluded from every analytics query,
so it disappears from spend rather than showing up wrong.

The gateway re-reads the file every `ACPI_REFRESH_SECONDS` (default 1h), so an
hourly sync needs no restart locally, where compose mounts
`apps/gateway/data/` read-only over `/data`. The same directory is inside the
image's build context and is COPYied in, which is what platforms that run the
image without a mount (Railway) load from — there, refreshing means redeploying.

`acpi_score` is the index's **P1 intelligence-per-dollar** metric carried
through verbatim, not a rescaled 0–10 rating. Models without benchmark data
carry `null`.

## Performance rules this code follows

- Nothing Tokenix wants to know may delay what the customer asked for. Pricing
  and ledger writes happen after the response is handed back, and any failure
  in them is logged and swallowed.
- Ledger writes are queued and batched. On overflow they are **dropped**, not
  backpressured — losing a row beats slowing an inference request.
- Streaming responses are never buffered; chunks are translated and forwarded
  as they arrive, with usage accumulated alongside.
- A cancelled stream still bills for tokens already produced (the recorder runs
  in a `finally`).

## Tests

```bash
cd apps/gateway
pip install -r requirements.txt
pytest tests -q          # 36 tests, no database or network required
```

`tests/test_proxy.py::test_request_flows_through_and_is_priced` is the MVP
acceptance test: one request in, correct response out, exactly one priced row
in the ledger.

## Deployment

Railway for `gateway` and `analytics` plus TimescaleDB; Vercel for the
dashboard. Required gateway env vars: `DATABASE_URL`, `ENCRYPTION_KEY`,
`ADMIN_TOKEN`, `ACPI_PRICES_PATH`, and optionally `REDIS_URL` (without it the
key cache is per-process, which is correct but not shared across containers).

Run one worker per container: the ledger queue and ACPI cache are per-process,
so scale out with containers, not workers.
