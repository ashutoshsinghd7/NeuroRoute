from src.gateway.filter import hard_filter
from src.gateway.ranking import rank
from tests.test_filter import models, req


def order(pref):
    valid, _ = hard_filter(models(), req(), 50, 1)
    return [m["name"] for m in rank(valid, pref)]


def test_cheapest_first():
    assert order("cheapest")[0] == "local-llama"      # costs 0


def test_fastest_first():
    assert order("fastest")[0] == "nano-1"


def test_best_quality_first():
    assert order("highest_quality")[0] == "max-5"


def test_rank_never_adds_rejected_models():
    valid, c = hard_filter(models(), req(max_latency_ms=1000), 50, 1)
    names = {m["name"] for m in rank(valid, "cheapest")}
    assert names == {x["model"] for x in c if x["valid"]}
