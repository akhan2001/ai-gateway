"""scripts/calibrate_anthropic_split.py, offline: a fake count_tokens stands in for Anthropic."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "calibrate", Path(__file__).parents[1] / "scripts" / "calibrate_anthropic_split.py"
)
calibrate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(calibrate)


def fake_count(body: dict) -> int:
    """Tokenizes the system prompt and messages at different rates, with a
    per-message and per-request overhead: what the character share misses."""
    system = len(body.get("system") or "") / 4.6
    messages = sum(len(m["content"]) / 3.4 + 4 for m in body["messages"])
    return round(7 + system + messages)


def test_sections_from_differences_sum_to_the_billed_count():
    for payload in calibrate.samples()[:6]:
        truth = calibrate.true_sections(payload, fake_count, "m")
        assert sum(truth) == fake_count(calibrate._body(payload["messages"], "m"))
        assert truth[1] == 0 and all(t >= 0 for t in truth)


def test_fitted_weights_cut_the_held_out_error():
    result = calibrate.calibrate(fake_count, "m")
    weights = result["weights"]
    assert weights[0] == 1.0 and weights[1] == 1.0  # system is the base; tools never sent
    assert weights[2] > 1.0  # messages tokenize denser than the system prompt here
    before, after = sum(result["before"]), sum(result["after"])
    assert after < before / 2, (result["before"], result["after"])


def test_samples_are_synthetic_deterministic_and_plain_text():
    a, b = calibrate.samples(), calibrate.samples()
    assert a == b and len(a) == 36
    for payload in a:
        for m in payload["messages"]:
            assert m["role"] in {"system", "user", "assistant"} and isinstance(m["content"], str)
    assert json.dumps(a)  # nothing but JSON-safe generated text
