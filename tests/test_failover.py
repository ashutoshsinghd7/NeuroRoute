import asyncio

from src.gateway.failover import run_with_failover


def m(name, latency=10):
    return {"name": name, "latency_ms": latency, "failure_rate": 0.0, "status": "up"}


def test_error_moves_to_next_model():
    async def call(model, prompt):
        if model["name"] == "a":
            raise RuntimeError("boom")
        return "ok"
    chosen, ans, attempts, _ = asyncio.run(run_with_failover([m("a"), m("b")], "hi", 1000, call))
    assert chosen["name"] == "b" and [a["status"] for a in attempts] == ["error", "success"]


def test_slow_model_times_out_and_budget_is_rechecked():
    async def call(model, prompt):
        await asyncio.sleep(1)
        return "late"
    chosen, _, attempts, _ = asyncio.run(run_with_failover([m("a"), m("b")], "hi", 200, call))
    assert chosen is None
    assert attempts[0]["status"] == "timeout"
    assert attempts[1]["status"] == "skipped"          # no time left for the backup


def test_only_listed_models_are_ever_called():
    seen = []
    async def call(model, prompt):
        seen.append(model["name"])
        raise RuntimeError("fail")
    asyncio.run(run_with_failover([m("a"), m("b")], "hi", 1000, call))
    assert seen == ["a", "b"]
