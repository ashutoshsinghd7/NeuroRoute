"""CP1 tests: POST /route validates requests correctly.

Two cases from the spec:
  - A good request returns HTTP 200 with {"status": "accepted"}.
  - max_cost = -5 returns HTTP 422 (validation error).
"""
import pytest
from fastapi.testclient import TestClient

from src.gateway.main import app

client = TestClient(app)

# A request that meets every rule.
GOOD_REQUEST = {
    "prompt": "Summarise this contract in three bullet points.",
    "max_latency_ms": 2000,
    "max_cost": 0.50,
    "min_capability": 3,
    "required_context": 20000,
    "preference": "cheapest",
}


def test_valid_request_is_accepted():
    """A well-formed request must return 200 and {"status": "accepted"}."""
    response = client.post("/route", json=GOOD_REQUEST)
    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}


def test_negative_max_cost_rejected():
    """max_cost must be positive; -5 must return a 422 validation error."""
    bad = {**GOOD_REQUEST, "max_cost": -5}
    response = client.post("/route", json=bad)
    assert response.status_code == 422


def test_negative_max_latency_rejected():
    """max_latency_ms must be positive; 0 must return a 422."""
    bad = {**GOOD_REQUEST, "max_latency_ms": 0}
    response = client.post("/route", json=bad)
    assert response.status_code == 422


def test_capability_out_of_range_rejected():
    """min_capability must be 1–5; 6 must return a 422."""
    bad = {**GOOD_REQUEST, "min_capability": 6}
    response = client.post("/route", json=bad)
    assert response.status_code == 422


def test_invalid_preference_rejected():
    """preference must be cheapest/fastest/highest_quality; anything else gives a 422."""
    bad = {**GOOD_REQUEST, "preference": "random"}
    response = client.post("/route", json=bad)
    assert response.status_code == 422


def test_required_context_can_be_omitted():
    """required_context is optional; omitting it should still return 200."""
    no_ctx = {k: v for k, v in GOOD_REQUEST.items() if k != "required_context"}
    response = client.post("/route", json=no_ctx)
    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}


def test_empty_prompt_rejected():
    """An empty prompt string must return a 422."""
    bad = {**GOOD_REQUEST, "prompt": ""}
    response = client.post("/route", json=bad)
    assert response.status_code == 422
