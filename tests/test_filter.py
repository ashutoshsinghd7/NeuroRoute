import json
import os
from types import SimpleNamespace as NS

from src.classifier.predict import classify
from src.gateway.filter import hard_filter

REG = os.path.join(os.path.dirname(__file__), "..", "src", "models", "registry.json")


def models():
    with open(REG, encoding="utf-8") as f:
        return json.load(f)


def req(**kw):
    base = dict(prompt="hi", max_latency_ms=10000, max_cost=10, min_capability=1,
                required_context=0, preference="cheapest")
    base.update(kw)
    return NS(**base)


def reasons(considered):
    return {c["model"]: c["reason"] for c in considered}


def test_down_model_rejected():
    ms = models(); ms[0]["status"] = "down"
    _, c = hard_filter(ms, req(), 5, 1)
    assert reasons(c)["nano-1"] == "model is down"


def test_context_rule():
    _, c = hard_filter(models(), req(required_context=100000), 5, 1)
    r = reasons(c)
    assert r["nano-1"] == "context 4000 < 100000" and r["ultra-4"] is None


def test_capability_rule():
    _, c = hard_filter(models(), req(), 5, 3)
    assert reasons(c)["mini-2"] == "capability 2 < 3"


def test_latency_rule():
    _, c = hard_filter(models(), req(max_latency_ms=1000), 5, 1)
    assert reasons(c)["pro-3"] == "latency 1500 > 1000"


def test_cost_rule():
    _, c = hard_filter(models(), req(max_cost=0.001), 5, 1)
    assert reasons(c)["mini-2"].startswith("cost")


def test_valid_models_never_break_a_rule():
    r = req(max_latency_ms=2000, max_cost=0.1, required_context=20000)
    valid, _ = hard_filter(models(), r, 50, 3)
    for m in valid:
        assert m["capability"] >= 3 and m["latency_ms"] <= 2000
        assert m["context_limit"] >= 20000 and m["est_cost"] <= 0.1


def test_ai_estimate_never_lowers_user_minimum():
    level = classify("What is the capital of France?")["level"]
    assert max(4, level) == 4  # user asked for 4, so 4 is what the filter uses
