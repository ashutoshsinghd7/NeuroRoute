"""CP6: fake (simulated) AI models.

No internet and no real AI is used. A "call" just waits for a while,
then either returns a fake answer or raises an error.
Judges can change a model's latency, status or failure rate and see the effect.
"""
import asyncio
import random


class ModelError(Exception):
    """Raised when a simulated model fails (down, or a random failure)."""


def will_fail(model: dict, seed: int = 0, call_key: str = "") -> bool:
    """Decide if this call fails. The same inputs always give the same answer.

    - A model that is not "up" always fails.
    - Otherwise it fails with probability `failure_rate`, using a
      seeded random number so demos can be repeated.
    """
    if model.get("status", "up") != "up":
        return True
    rate = model.get("failure_rate", 0.0)
    rng = random.Random(f"{seed}|{call_key}|{model['name']}")
    return rng.random() < rate


async def call_simulated(
    model: dict, prompt: str, seed: int = 0, call_key: str = ""
) -> str:
    """Pretend to call a model.

    Waits `latency_ms` (+ optional `extra_delay_ms`), then answers or fails.
    - Down model: fails at once (a dead server answers fast with an error).
    - Random failure: happens AFTER the wait (the time is still spent).
    """
    if model.get("status", "up") != "up":
        raise ModelError("model is down")

    delay_ms = model["latency_ms"] + model.get("extra_delay_ms", 0)
    await asyncio.sleep(delay_ms / 1000)

    if will_fail(model, seed, call_key):
        raise ModelError("random failure")

    return f"[{model['name']}] simulated answer to: {prompt[:60]}"