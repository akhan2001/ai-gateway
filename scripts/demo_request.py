#!/usr/bin/env python3
"""Fire one live request through the gateway during a pitch, then show what
got recorded.

Correction vs. the original plan sketch: the gateway's proxy response is a
passthrough of the upstream provider's response (see
apps/gateway/src/providers/openai.py:parse_response, which returns `raw`
unchanged) — it does NOT include `cost` or `acpi` fields. Those only exist
once the request lands in usage_records, written server-side after the
upstream call completes (apps/gateway/src/routers/proxy.py:_record). So this
script does two calls: the live completion, then a read of what analytics-api
just logged for it, which is where the cost/ACPI numbers actually come from —
the same thing the dashboard's /dashboard/logs page reads.

Usage:
    python scripts/demo_request.py --api-key txk-... \\
        --gateway-url https://cozy-patience-production-815c.up.railway.app \\
        --analytics-url https://ai-gateway-production-a74e.up.railway.app

    # or against local docker-compose:
    python scripts/demo_request.py --api-key txk-...
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import httpx


def run(args: argparse.Namespace) -> int:
    api_key = args.api_key or os.getenv("TOKENIX_DEMO_KEY")
    if not api_key:
        print("Pass --api-key or set TOKENIX_DEMO_KEY (a txk- key from seed_demo_workspace.py).", file=sys.stderr)
        return 1

    gateway_url = args.gateway_url.rstrip("/")
    analytics_url = args.analytics_url.rstrip("/")

    path = {
        "openai": "openai/v1/chat/completions",
        "anthropic": "anthropic/v1/messages",
        "google": f"google/v1beta/models/{args.model}:generateContent",
    }[args.provider]

    body = {
        "openai": {
            "model": args.model,
            "messages": [{"role": "user", "content": args.prompt}],
            "max_tokens": 200,
        },
        "anthropic": {
            "model": args.model,
            "max_tokens": 200,
            "messages": [{"role": "user", "content": args.prompt}],
        },
        "google": {"contents": [{"parts": [{"text": args.prompt}]}]},
    }[args.provider]

    print(f"-> POST {gateway_url}/{path}")
    started = time.monotonic()
    with httpx.Client(timeout=60) as client:
        response = client.post(
            f"{gateway_url}/{path}",
            headers={"authorization": f"Bearer {api_key}", "tokenix-feature": "live-demo"},
            json=body,
        )
    latency_ms = int((time.monotonic() - started) * 1000)

    if response.status_code >= 400:
        print(f"gateway returned {response.status_code}: {response.text}", file=sys.stderr)
        return 1

    raw = response.json()
    print(f"<- {response.status_code} in {latency_ms}ms")
    print(json.dumps(raw, indent=2)[:1500])

    # Give the write path (async, batched — see WRITE_FLUSH_SECONDS in
    # apps/gateway/.env.template) a moment to land before reading it back.
    time.sleep(3)

    print(f"\n-> GET {analytics_url}/api/v1/logs?limit=1")
    with httpx.Client(timeout=30) as client:
        logs = client.get(
            f"{analytics_url}/api/v1/logs",
            headers={"authorization": f"Bearer {api_key}"},
            params={"limit": 1},
        )
    if logs.status_code != 200:
        print(f"could not read back the logged request ({logs.status_code}): {logs.text}", file=sys.stderr)
        return 1

    rows = logs.json().get("rows", [])
    if not rows:
        print("no row logged yet — the write queue may need another moment; re-run the GET manually.")
        return 0

    row = rows[0]
    print(f"cost_usd:       ${row['cost_usd']}")
    print(f"acpi_bench_usd: ${row['acpi_bench_usd']}")
    print(f"vs_acpi_pct:    {row['vs_acpi_pct']}%")
    print(f"latency_ms:     {row['latency_ms']}")
    print("\nThis is what /dashboard/logs shows after a refresh.")
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--api-key", default=None, help="txk- key (or $TOKENIX_DEMO_KEY).")
    p.add_argument("--gateway-url", default=os.getenv("TOKENIX_GATEWAY_URL", "http://localhost:8080"))
    p.add_argument("--analytics-url", default=os.getenv("TOKENIX_ANALYTICS_URL", "http://localhost:8001"))
    p.add_argument("--provider", choices=["openai", "anthropic", "google"], default="openai")
    p.add_argument("--model", default="gpt-4o-mini")
    p.add_argument("--prompt", default="What is Tokenix?")
    return p.parse_args()


if __name__ == "__main__":
    sys.exit(run(parse_args()))
