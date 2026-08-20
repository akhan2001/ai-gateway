"""Stand-in for the Tokenix analytics API, serving fixed demo data.

Lets the dashboard be developed and tested without TimescaleDB, the gateway,
or any real traffic. It implements the same five endpoints and the same
`txk-` bearer auth as apps/analytics-api, and nothing else.

    pip install fastapi uvicorn
    python -m uvicorn tools.stub_analytics:app --port 8001

Then point the dashboard at it:

    TOKENIX_ANALYTICS_URL=http://127.0.0.1:8001 npm run dev

and sign in on /connect with the key below.

NOT for deployment: the numbers are invented and the key is public.
"""

from datetime import date, timedelta

from fastapi import FastAPI, Header, HTTPException

app = FastAPI()
GOOD = "txk-test-key"


def check(auth: str | None) -> None:
    if not auth or auth.removeprefix("Bearer ").strip() != GOOD:
        raise HTTPException(status_code=401, detail="Invalid Tokenix API key")


@app.get("/api/v1/summary")
async def summary(authorization: str | None = Header(default=None)):
    check(authorization)
    return {
        "this_month_spend_usd": 47200.15,
        "last_month_spend_usd": 40012.4,
        "mom_change_pct": 17.96,
        "acpi_benchmark_usd": 43800.0,
        "vs_acpi_pct": 7.76,
        "top_model": {"model_id": "openai/gpt-4o", "cost_usd": 31044.2},
        "total_requests": 142000,
    }


@app.get("/api/v1/usage")
async def usage(days: int = 30, group_by: str = "day", authorization: str | None = Header(default=None)):
    check(authorization)
    start = date.today() - timedelta(days=days - 1)
    series = []
    for i in range(days):
        cost = 1200 + i * 42 + (i % 5) * 180
        series.append(
            {
                "day": (start + timedelta(days=i)).isoformat(),
                "requests": 3800 + i * 40,
                "input_tokens": 9_000_000 + i * 120_000,
                "output_tokens": 2_100_000 + i * 30_000,
                "cost_usd": round(cost, 4),
                "acpi_bench_usd": round(cost * 0.92, 4),
                "overpay_usd": round(cost * 0.08, 4),
            }
        )
    return {"days": days, "group_by": group_by, "series": series}


MODELS = [
    ("openai/gpt-4o", "openai", 38200.0, 34100.0, 4.1),
    ("anthropic/claude-haiku-4-5", "anthropic", 6100.0, 6420.0, 5.8),
    ("google/gemini-2.5-flash", "google", 1980.0, 2510.0, 6.8),
    ("deepseek/deepseek-v3", "deepseek", 920.0, 2770.0, 7.2),
]


@app.get("/api/v1/models")
async def models(days: int = 30, authorization: str | None = Header(default=None)):
    check(authorization)
    return {
        "days": days,
        "models": [
            {
                "model_id": m,
                "provider": p,
                "requests": 41000,
                "input_tokens": 210_000_000,
                "output_tokens": 48_000_000,
                "cost_usd": c,
                "acpi_bench_usd": b,
                "overpay_usd": round(c - b, 4),
                "acpi_score": s,
            }
            for m, p, c, b, s in MODELS
        ],
    }


@app.get("/api/v1/benchmark")
async def benchmark(days: int = 30, authorization: str | None = Header(default=None)):
    check(authorization)
    rows = []
    for m, p, c, b, s in MODELS:
        over = c - b
        rows.append(
            {
                "model_id": m,
                "provider": p,
                "requests": 41000,
                "input_tokens": 210_000_000,
                "output_tokens": 48_000_000,
                "tokens": 258_000_000,
                "cost_usd": c,
                "acpi_bench_usd": b,
                "overpay_usd": round(over, 4),
                "overpay_pct": round(over / b * 100, 2),
                "acpi_score": s,
                "status": "above_market" if over > 0 else "below_market",
            }
        )
    total_c = sum(r["cost_usd"] for r in rows)
    total_b = sum(r["acpi_bench_usd"] for r in rows)
    return {
        "days": days,
        "total_cost_usd": total_c,
        "acpi_benchmark_usd": total_b,
        "overpay_usd": round(total_c - total_b, 4),
        "overpay_pct": round((total_c - total_b) / total_b * 100, 2),
        "models": rows,
        "opportunities": [
            {
                "model_id": "openai/gpt-4o",
                "action": "openai/gpt-4o costs 12% above the ACPI market rate. Moving this workload to a cheaper model at similar quality would recover about $4100.00 over the last 30 days.",
                "potential_saving_usd": 4100.0,
            }
        ],
    }


@app.get("/api/v1/forecast")
async def forecast(authorization: str | None = Header(default=None)):
    check(authorization)
    return {
        "method": "month-to-date run rate, MoM growth compounded forward",
        "current_month_to_date_usd": 30680.1,
        "projected_this_month_usd": 51400.0,
        "last_month_usd": 43559.3,
        "mom_growth_pct": 18.0,
        "projected_rest_of_year_usd": 847000.0,
        "projected_with_optimization_usd": 583000.0,
        "potential_saving_usd": 264000.0,
        "months_of_history": 4,
        "low_confidence": False,
    }
