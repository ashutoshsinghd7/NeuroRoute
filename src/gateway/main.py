"""FastAPI application — the one public door for NeuroRoute.

CP1: validate input and return {"status": "accepted"}.
Routing logic (filter, rank, call, failover) is added in later checkpoints.
"""
from fastapi import FastAPI

from src.gateway.schemas import RouteRequest

app = FastAPI(
    title="NeuroRoute",
    description="Cost-, latency- and quality-aware AI model router (A-01).",
    version="0.1.0",
)


@app.post("/route")
async def route(request: RouteRequest) -> dict:
    """Accept a routing request and validate it.

    FastAPI + Pydantic check every field automatically.
    If any field is invalid (e.g. max_cost=-5), FastAPI returns a 422 error
    before this function is even called.

    Returns {"status": "accepted"} for a valid request.
    Routing logic will be wired in from CP2 onwards.
    """
    # Placeholder — real routing is added in CP2 and beyond.
    return {"status": "accepted"}
