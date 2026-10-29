"""Fit ANTHROPIC_SECTION_WEIGHTS (services/anatomy.py) from Anthropic's own token counts.

    ANTHROPIC_API_KEY=... python scripts/calibrate_anthropic_split.py [--model claude-sonnet-5-5]

Synthetic prompts only: every sample is generated in this file from fixed
seeds. Nothing is read from the database or from customer traffic. It calls
the free count_tokens endpoint only; no messages are generated or billed.

True section tokens per sample come from counting the request with sections
added one at a time:

    C = a one-word user turn alone       (fixed per-request overhead)
    B = system + that turn                system  = B - C
    D = system + history + that turn      history = D - B
    E = the full request                  latest  = E - D + C  (absorbs the overhead)

The three sum to E, which is what Anthropic bills. Tools are always 0: the
gateway's Anthropic adapter doesn't forward tool definitions.

The weights are fitted on half the samples; the error is reported on the other
half, before (equal weights, today's estimate) and after. Paste the printed
tuple into ANTHROPIC_SECTION_WEIGHTS, then rows are marked 'calibrated'.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import uuid
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.providers.anthropic import AnthropicAdapter  # noqa: E402
from src.services.anatomy import measure, scale_to_total  # noqa: E402

SECTIONS = ("system", "tools", "history", "latest")
_STUB = {"role": "user", "content": "x"}
_WS = uuid.UUID(int=0)

Counter = Callable[[dict[str, Any]], int]

_WORDS = (
    "order refund shipping account invoice policy customer agent escalate verify address warehouse "
    "delivery tracking return damaged replacement discount subscription renewal billing cycle plan"
).split()
_CODE = [
    "def total(items):\n    return sum(i.price * i.qty for i in items)\n",
    "for (const row of rows) {\n  if (!row.ok) throw new Error(`bad row ${row.id}`);\n}\n",
    "SELECT id, SUM(amount) FROM payments WHERE status = 'paid' GROUP BY id;\n",
]


def _prose(rng: random.Random, words: int) -> str:
    sentences, out = [], []
    for _ in range(words):
        out.append(rng.choice(_WORDS))
        if len(out) >= rng.randint(8, 16):
            sentences.append(" ".join(out).capitalize() + ".")
            out = []
    return " ".join(sentences + ([" ".join(out).capitalize() + "."] if out else []))


def _json_blob(rng: random.Random, rows: int) -> str:
    return json.dumps({"results": [{"id": f"R-{rng.randint(1000, 9999)}", "status": rng.choice(["ok", "late", "lost"]),
                                    "eta_days": rng.randint(1, 9)} for _ in range(rows)]}, indent=2)


def samples() -> list[dict[str, Any]]:
    """OpenAI-shaped, plain-text conversations: the shape the Anthropic route accepts."""
    out = []
    kinds = ("support", "coding", "rag", "json", "long_history", "short")
    for i in range(36):
        kind = kinds[i % len(kinds)]
        rng = random.Random(i)
        system = _prose(rng, rng.randint(150, 900))
        turns = {"short": 1, "long_history": rng.randint(12, 20)}.get(kind, rng.randint(2, 6))
        messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for _ in range(turns - 1):
            messages.append({"role": "user", "content": _prose(rng, rng.randint(10, 60))})
            reply = rng.choice(_CODE) * rng.randint(1, 4) if kind == "coding" else _prose(rng, rng.randint(20, 120))
            messages.append({"role": "assistant", "content": reply})
        latest = {
            "rag": "Use these documents:\n\n" + "\n\n".join(_prose(rng, 300) for _ in range(rng.randint(2, 5))),
            "json": "Here is the lookup result:\n" + _json_blob(rng, rng.randint(5, 40)),
            "coding": "Fix this:\n" + rng.choice(_CODE) * rng.randint(2, 8),
        }.get(kind, _prose(rng, rng.randint(10, 80)))
        messages.append({"role": "user", "content": latest})
        out.append({"model": "calibration", "messages": messages})
    return out


def _body(messages: list[dict[str, Any]], model: str) -> dict[str, Any]:
    body = AnthropicAdapter().build_request("v1/chat/completions", {"messages": messages}, "unused").payload
    body.pop("max_tokens", None)
    return {**body, "model": model}


def true_sections(payload: dict[str, Any], count: Counter, model: str) -> tuple[int, int, int, int]:
    messages = payload["messages"]
    system = [m for m in messages if m["role"] == "system"]
    conversation = [m for m in messages if m["role"] != "system"]
    last_assistant = max((i for i, m in enumerate(conversation) if m["role"] == "assistant"), default=-1)
    history = conversation[: last_assistant + 1]

    c = count(_body([_STUB], model))
    b = count(_body(system + [_STUB], model))
    d = count(_body(system + history + [_STUB], model))
    e = count(_body(messages, model))
    return b - c, 0, d - b, e - d + c


def estimated_sections(payload: dict[str, Any], total: int, weights: tuple[float, ...]) -> tuple[int, int, int, int]:
    a = measure(payload, _WS, forwards_tools=False)
    chars = (a.system_chars, a.tool_chars, a.history_chars, a.user_chars)
    return scale_to_total(tuple(c * w for c, w in zip(chars, weights)), total)


def fit_weights(rows: list[tuple[dict[str, Any], tuple[int, ...]]]) -> tuple[float, float, float, float]:
    """Tokens per character for each section, relative to the system prompt."""
    chars = [0, 0, 0, 0]
    tokens = [0, 0, 0, 0]
    for payload, truth in rows:
        a = measure(payload, _WS, forwards_tools=False)
        for i, c in enumerate((a.system_chars, a.tool_chars, a.history_chars, a.user_chars)):
            chars[i] += c
            tokens[i] += truth[i]
    rates = [t / c if c else None for t, c in zip(tokens, chars)]
    base = rates[0] or next(r for r in rates if r)
    w = tuple(round(r / base, 3) if r else 1.0 for r in rates)
    return w[0], w[1], w[2], w[3]


def error_points(rows: list[tuple[dict[str, Any], tuple[int, ...]]], weights: tuple[float, ...]) -> list[float]:
    """Mean absolute error per section, in percentage points of the request's total."""
    errs = [0.0, 0.0, 0.0, 0.0]
    for payload, truth in rows:
        total = sum(truth)
        est = estimated_sections(payload, total, weights)
        for i in range(4):
            errs[i] += abs(est[i] - truth[i]) / total * 100
    return [e / len(rows) for e in errs]


def calibrate(count: Counter, model: str) -> dict[str, Any]:
    rows = [(p, true_sections(p, count, model)) for p in samples()]
    fit, held_out = rows[0::2], rows[1::2]
    weights = fit_weights(fit)
    return {
        "weights": weights,
        "before": error_points(held_out, (1.0, 1.0, 1.0, 1.0)),
        "after": error_points(held_out, weights),
        "samples": len(rows),
    }


def anthropic_counter(api_key: str) -> Counter:
    import httpx

    client = httpx.Client(
        base_url="https://api.anthropic.com",
        headers={"x-api-key": api_key, "anthropic-version": AnthropicAdapter.api_version},
        timeout=30,
    )

    def count(body: dict[str, Any]) -> int:
        response = client.post("/v1/messages/count_tokens", json=body)
        response.raise_for_status()
        return int(response.json()["input_tokens"])

    return count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--model", default="claude-sonnet-5-5")
    args = parser.parse_args()
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        print("Set ANTHROPIC_API_KEY.")
        return 2

    result = calibrate(anthropic_counter(api_key), args.model)
    print(f"{result['samples']} synthetic samples on {args.model}; error on the held-out half,")
    print("mean absolute, in percentage points of each request's total:\n")
    print(f"  {'section':<9}{'before':>9}{'after':>9}")
    for name, before, after in zip(SECTIONS, result["before"], result["after"]):
        if name == "tools":
            continue  # never forwarded to Anthropic
        print(f"  {name:<9}{before:>8.2f}%{after:>8.2f}%")
    print(f"\nANTHROPIC_SECTION_WEIGHTS = {result['weights']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
