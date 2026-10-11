"""CP6 tests: simulated models and the call-with-timeout function.
(CP7 will add failover tests to this same file.)
"""
import asyncio

from src.gateway.failover import call_with_timeout
from src.models.simulated import will_fail


def make_model(**changes) -> dict:
    model = {"name": "test-1", "capability": 3, "context_limit": 64000,
             "cost_per_1k": 0.30, "latency_ms": 50, "failure_rate": 0.0,
             "status": "up"}
    model.update(changes)
    return model


def run(model, remaining_ms=1000, seed=0, key="r1"):
    return asyncio.run(call_with_timeout(model, "hello", remaining_ms, seed, key))


def test_normal_model_succeeds():
    r = run(make_model())
    assert r["status"] == "success"
    assert "test-1" in r["answer"]


def test_down_model_gives_error_fast():
    r = run(make_model(status="down"))
    assert r["status"] == "error"
    assert r["ms"] < 40            # fails at once, does not wait


def test_slow_model_times_out():
    # artificial 5 s delay, 200 ms budget -> timeout
    r = run(make_model(extra_delay_ms=5000), remaining_ms=200)
    assert r["status"] == "timeout"
    assert r["ms"] < 1000          # we did not wait the full 5 s


def test_no_time_left_does_not_call_model():
    r = run(make_model(), remaining_ms=0)
    assert r["status"] == "timeout"
    assert r["ms"] == 0


def test_failure_rate_one_always_errors():
    assert run(make_model(failure_rate=1.0))["status"] == "error"


def test_failure_rate_zero_never_errors():
    assert all(not will_fail(make_model(), 0, f"r{i}") for i in range(50))


def test_failures_are_repeatable_with_same_seed():
    m = make_model(failure_rate=0.5)
    first = [will_fail(m, 42, f"r{i}") for i in range(30)]
    second = [will_fail(m, 42, f"r{i}") for i in range(30)]
    assert first == second
    assert any(first) and not all(first)   # a mix, so 0.5 really works


def test_edited_latency_is_used_immediately():
    # Same model, judge changes latency 50 -> 300; budget is 150 ms.
    assert run(make_model(latency_ms=50), remaining_ms=150)["status"] == "success"
    assert run(make_model(latency_ms=300), remaining_ms=150)["status"] == "timeout"