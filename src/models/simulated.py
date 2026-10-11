"""Fake (simulated) AI models. No internet, no paid API.

A call waits `latency_ms`, then either answers or raises an error.
Failures are SEEDED: same model + same prompt + same seed = same outcome (reproducible demo).
Change the seed with:  NEUROROUTE_SEED=7 uvicorn src.gateway.main:app
"""
import asyncio
import os
import random

SEED = int(os.environ.get("NEUROROUTE_SEED", "42"))


class SimulatedModelError(Exception):
    """Raised when a simulated model is down or randomly fails."""


async def call_model(model: dict, prompt: str) -> str:
    if model["status"] != "up":
        raise SimulatedModelError(f"{model['name']} is down")
    await asyncio.sleep(model["latency_ms"] / 1000)  # pretend to think
    rng = random.Random(f"{SEED}:{model['name']}:{prompt}")  # seeded -> repeatable
    if rng.random() < model["failure_rate"]:
        raise SimulatedModelError(f"{model['name']} failed (simulated)")
    return f"[{model['name']}] Simulated answer to: {prompt[:60]}"

# --- REAL LOCAL MODEL (OPTIONAL, commented out) -------------------------------
# To call a real small model through Ollama, install Ollama, run `ollama pull llama3.2`,
# then uncomment this and use it for the model named "local-llama" inside call_model().
#
# import httpx
# async def call_ollama(prompt: str) -> str:
#     async with httpx.AsyncClient(timeout=60) as client:
#         r = await client.post("http://localhost:11434/api/generate",
#                               json={"model": "llama3.2", "prompt": prompt, "stream": False})
#         return r.json()["response"]
