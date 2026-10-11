import asyncio
from tests.test_filter import models, req
from src.gateway.router import route_request


def test_impossible_request_is_refused_and_nothing_is_called():
    called = []
    async def call(model, prompt):
        called.append(model["name"])
        return "x"
    r = req(prompt="Prove this theorem and analyse the legal risk.", max_latency_ms=100,
            min_capability=5, max_cost=0.1)
    out = asyncio.run(route_request(r, models(), call))
    assert out["status"] == "NO_VALID_MODEL" and called == [] and out["rejected"]


def test_all_valid_models_failing_is_a_refusal_without_relaxing_rules():
    tried = []
    async def call(model, prompt):
        tried.append(model["name"])
        raise RuntimeError("down")
    out = asyncio.run(route_request(req(max_latency_ms=1000), models(), call))
    valid = {c["model"] for c in out["trace"]["considered"] if c["valid"]}
    assert out["status"] == "NO_VALID_MODEL" and set(tried) <= valid


def test_same_request_gives_same_decision():
    fast = models()
    for x in fast:
        x["latency_ms"] = 10
    a = asyncio.run(route_request(req(prompt="What is the capital of France?"), fast))
    b = asyncio.run(route_request(req(prompt="What is the capital of France?"), fast))
    assert a["chosen_model"] == b["chosen_model"] and a["trace"]["considered"] == b["trace"]["considered"]


def test_vague_prompt_is_flagged_low_confidence():
    out = asyncio.run(route_request(req(prompt="Help me with this thing"), models()))
    assert out["trace"]["complexity"]["low_confidence_routing"] is True
