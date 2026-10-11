"""CP6 (call with timeout). CP7 will add the failover loop to this file.

This file never decides which model is valid. It only calls the model
it is given. The caller must pass a model from the already validated,
already ranked list (AGENT.md rules 3 and 5).
"""
import asyncio
import time

from src.models.simulated import ModelError, call_simulated


async def call_with_timeout(
    model: dict,
    prompt: str,
    remaining_ms: float,
    seed: int = 0,
    call_key: str = "",
) -> dict:
    """Call one model, but never wait longer than `remaining_ms`.

    Returns an attempt record:
      {"model", "status": "success|error|timeout", "ms", "answer", "error"}
    "Too slow" is treated as a failure, the same as a crash.
    """
    name = model["name"]

    # No time left: do not even call the model.
    if remaining_ms <= 0:
        return {"model": name, "status": "timeout", "ms": 0,
                "answer": None, "error": "no time left"}

    start = time.perf_counter()
    answer, error = None, None
    try:
        answer = await asyncio.wait_for(
            call_simulated(model, prompt, seed, call_key),
            timeout=remaining_ms / 1000,
        )
        status = "success"
    except asyncio.TimeoutError:
        status = "timeout"
        error = f"no answer within {round(remaining_ms)} ms"
    except ModelError as e:
        status = "error"
        error = str(e)

    ms = round((time.perf_counter() - start) * 1000)
    return {"model": name, "status": status, "ms": ms,
            "answer": answer, "error": error}