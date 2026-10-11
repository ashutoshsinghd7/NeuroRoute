"""CALL + TIMEOUT + FAILOVER (CP6, CP7).

We walk down the ranked list of VALID models. Nothing is ever re-filtered or relaxed.
Before each try we re-check the time left. Slow = failure (timeout).
"""
import asyncio
import time

from src.models.simulated import call_model


async def run_with_failover(ranked, prompt, max_latency_ms, call=call_model):
    """Return (chosen_model, answer, attempts, total_ms). chosen_model is None if all failed."""
    attempts = []
    start = time.monotonic()
    for m in ranked:
        used = int((time.monotonic() - start) * 1000)
        remaining = max_latency_ms - used
        if m["latency_ms"] > remaining:  # a backup must still fit the time that is left
            attempts.append({"model": m["name"], "status": "skipped", "ms": 0,
                             "reason": "not enough time left"})
            continue
        t0 = time.monotonic()
        try:
            answer = await asyncio.wait_for(call(m, prompt), timeout=remaining / 1000)
            ms = int((time.monotonic() - t0) * 1000)
            attempts.append({"model": m["name"], "status": "success", "ms": ms})
            return m, answer, attempts, int((time.monotonic() - start) * 1000)
        except asyncio.TimeoutError:
            attempts.append({"model": m["name"], "status": "timeout",
                             "ms": int((time.monotonic() - t0) * 1000)})
        except Exception as e:  # any model error counts as a failure
            attempts.append({"model": m["name"], "status": "error",
                             "ms": int((time.monotonic() - t0) * 1000), "reason": str(e)})
    return None, None, attempts, int((time.monotonic() - start) * 1000)
