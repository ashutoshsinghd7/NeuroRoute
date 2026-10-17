"""Tests for Milestone D-FIX: Status preservation on 429/500, 401 auth down marking, exact 4 trace.usage keys, and key redaction."""
import logging
import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from src.gateway.db import init_workspace_schema
from src.gateway.live_call import rate_limiter
from src.gateway.logger import init_db
from src.gateway.main import app


@pytest.fixture(autouse=True)
def setup_test_env(tmp_path, monkeypatch):
    """Set up isolated DB and valid secret key."""
    db_file = str(tmp_path / "test_byok_fix.db")
    monkeypatch.setenv("NEUROROUTE_DB", db_file)
    fernet_key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("NEUROROUTE_SECRET_KEY", fernet_key)
    init_db()
    init_workspace_schema()
    rate_limiter.reset()
    yield db_file


@pytest.fixture
def client():
    return TestClient(app)


def test_429_leaves_model_up_and_retried_next_request(client, monkeypatch):
    """A 429 from model A leaves A's status 'up' and A is tried again on the next request."""
    call_count = {"count": 0}

    def mock_handler(request: httpx.Request):
        call_count["count"] += 1
        if call_count["count"] == 1:
            return httpx.Response(429, json={"error": {"message": "Rate limited"}})
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "Success from Model A on retry"}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            },
        )

    orig_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *args, **kwargs: orig_client(transport=httpx.MockTransport(mock_handler)),
    )

    # Slot 1: Model A (live, fast 200ms)
    client.post(
        "/api/workspace/models/1",
        json={
            "model_name": "model-a-live",
            "model_id": "gpt-4o-mini",
            "execution_mode": "live",
            "provider": "openai",
            "api_key": "sk-key-a",
            "capability": 3,
            "context_limit": 8000,
            "cost_per_1k_input": 0.01,
            "cost_per_1k_output": 0.02,
            "latency_ms": 200,
        },
    )

    # Slot 2: Model B (simulated backup, slower 500ms)
    client.post(
        "/api/workspace/models/2",
        json={
            "model_name": "model-b-simulated",
            "model_id": "backup-v1",
            "execution_mode": "simulated",
            "provider": "simulated",
            "capability": 3,
            "context_limit": 8000,
            "cost_per_1k": 0.05,
            "latency_ms": 500,
            "status": "up",
        },
    )

    req = {
        "prompt": "Test 429 failover",
        "max_latency_ms": 2000,
        "max_cost": 1.0,
        "min_capability": 1,
        "required_context": 100,
        "preference": "fastest",
    }

    # Request 1: Model A returns 429 -> fails over to Model B
    resp1 = client.post("/route", json=req)
    assert resp1.status_code == 200
    assert resp1.json()["chosen_model"] == "model-b-simulated"

    # Status of Model A MUST remain 'up'
    slot1 = client.get("/api/workspace/models").json()["slots"][0]["model"]
    assert slot1["status"] == "up"
    assert slot1["status_reason"] is None

    # Request 2: Model A is tried again and succeeds
    resp2 = client.post("/route", json=req)
    assert resp2.status_code == 200
    assert resp2.json()["chosen_model"] == "model-a-live"
    assert resp2.json()["answer"] == "Success from Model A on retry"


def test_500_leaves_model_up_and_retried_next_request(client, monkeypatch):
    """A 500 from model A leaves A's status 'up' and A is tried again on the next request."""
    call_count = {"count": 0}

    def mock_handler(request: httpx.Request):
        call_count["count"] += 1
        if call_count["count"] == 1:
            return httpx.Response(500, json={"error": {"message": "Internal error"}})
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "Success from Model A after 500"}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            },
        )

    orig_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *args, **kwargs: orig_client(transport=httpx.MockTransport(mock_handler)),
    )

    # Slot 1: Model A (live, fast 200ms)
    client.post(
        "/api/workspace/models/1",
        json={
            "model_name": "model-a-live",
            "model_id": "gpt-4o-mini",
            "execution_mode": "live",
            "provider": "openai",
            "api_key": "sk-key-a",
            "capability": 3,
            "context_limit": 8000,
            "cost_per_1k_input": 0.01,
            "cost_per_1k_output": 0.02,
            "latency_ms": 200,
        },
    )

    # Slot 2: Model B (simulated backup, slower 500ms)
    client.post(
        "/api/workspace/models/2",
        json={
            "model_name": "model-b-simulated",
            "model_id": "backup-v1",
            "execution_mode": "simulated",
            "provider": "simulated",
            "capability": 3,
            "context_limit": 8000,
            "cost_per_1k": 0.05,
            "latency_ms": 500,
            "status": "up",
        },
    )

    req = {
        "prompt": "Test 500 failover",
        "max_latency_ms": 2000,
        "max_cost": 1.0,
        "min_capability": 1,
        "required_context": 100,
        "preference": "fastest",
    }

    # Request 1: Model A returns 500 -> fails over to Model B
    resp1 = client.post("/route", json=req)
    assert resp1.status_code == 200
    assert resp1.json()["chosen_model"] == "model-b-simulated"

    # Status of Model A MUST remain 'up'
    slot1 = client.get("/api/workspace/models").json()["slots"][0]["model"]
    assert slot1["status"] == "up"

    # Request 2: Model A is retried and succeeds
    resp2 = client.post("/route", json=req)
    assert resp2.status_code == 200
    assert resp2.json()["chosen_model"] == "model-a-live"


def test_401_sets_model_down_with_invalid_api_key(client, monkeypatch):
    """A 401 sets A down with status_reason 'invalid API key' and failover reaches model B."""
    def mock_handler(request: httpx.Request):
        return httpx.Response(401, json={"error": {"message": "Unauthorized"}})

    orig_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *args, **kwargs: orig_client(transport=httpx.MockTransport(mock_handler)),
    )

    client.post(
        "/api/workspace/models/1",
        json={
            "model_name": "auth-fail-model",
            "model_id": "gpt-4o-mini",
            "execution_mode": "live",
            "provider": "openai",
            "api_key": "bad-key",
            "capability": 3,
            "context_limit": 8000,
            "cost_per_1k_input": 0.01,
            "cost_per_1k_output": 0.02,
            "latency_ms": 200,
        },
    )

    client.post(
        "/api/workspace/models/2",
        json={
            "model_name": "model-b-simulated",
            "model_id": "backup-v1",
            "execution_mode": "simulated",
            "provider": "simulated",
            "capability": 3,
            "context_limit": 8000,
            "cost_per_1k": 0.05,
            "latency_ms": 500,
            "status": "up",
        },
    )

    req = {
        "prompt": "Test 401 failover",
        "max_latency_ms": 2000,
        "max_cost": 1.0,
        "min_capability": 1,
        "required_context": 100,
        "preference": "fastest",
    }

    resp = client.post("/route", json=req)
    assert resp.status_code == 200
    assert resp.json()["chosen_model"] == "model-b-simulated"

    # Status of Model A MUST be 'down' with status_reason 'invalid API key'
    slot1 = client.get("/api/workspace/models").json()["slots"][0]["model"]
    assert slot1["status"] == "down"
    assert slot1["status_reason"] == "invalid API key"


def test_trace_usage_has_exact_four_keys(client, monkeypatch):
    """trace.usage must be exactly {"input_tokens", "output_tokens", "actual_cost", "latency_ms"} in rupees."""
    def mock_handler(request: httpx.Request):
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "Answer with usage"}}],
                "usage": {"prompt_tokens": 200, "completion_tokens": 100, "total_tokens": 300},
            },
        )

    orig_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *args, **kwargs: orig_client(transport=httpx.MockTransport(mock_handler)),
    )

    # ₹0.02 per 1k input, ₹0.04 per 1k output
    client.post(
        "/api/workspace/models/1",
        json={
            "model_name": "usage-test-model",
            "model_id": "gpt-4o-mini",
            "execution_mode": "live",
            "provider": "openai",
            "api_key": "test-key",
            "capability": 3,
            "context_limit": 8000,
            "cost_per_1k_input": 0.02,
            "cost_per_1k_output": 0.04,
            "latency_ms": 300,
        },
    )

    req = {
        "prompt": "Usage test",
        "max_latency_ms": 2000,
        "max_cost": 1.0,
        "min_capability": 1,
        "required_context": 100,
    }

    resp = client.post("/route", json=req)
    assert resp.status_code == 200
    data = resp.json()
    assert "usage" in data["trace"]
    usage = data["trace"]["usage"]

    # Must be EXACTLY these 4 keys
    expected_keys = {"input_tokens", "output_tokens", "actual_cost", "latency_ms"}
    assert set(usage.keys()) == expected_keys
    assert len(usage) == 4

    assert usage["input_tokens"] == 200
    assert usage["output_tokens"] == 100
    # Expected cost: (200/1000)*0.02 + (100/1000)*0.04 = 0.004 + 0.004 = 0.008 rupees
    assert abs(usage["actual_cost"] - 0.008) < 1e-6
    assert abs(data["trace"]["cost"] - 0.008) < 1e-6

    # Verify deprecated/removed keys do NOT exist
    for forbidden_key in ("cost_usd", "prompt_tokens", "completion_tokens", "total_tokens"):
        assert forbidden_key not in usage


def test_api_key_not_in_response_history_or_caplog(client, caplog, monkeypatch):
    """The test key string appears in no response, no history row and no caplog output."""
    caplog.set_level(logging.DEBUG)
    test_key = "sk-secret-xyz-unique-sentinel-9999"

    def mock_handler(request: httpx.Request):
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "Normal response"}}],
                "usage": {"prompt_tokens": 50, "completion_tokens": 25, "total_tokens": 75},
            },
        )

    orig_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *args, **kwargs: orig_client(transport=httpx.MockTransport(mock_handler)),
    )

    # 1. Create live slot
    post_resp = client.post(
        "/api/workspace/models/1",
        json={
            "model_name": "secret-model",
            "model_id": "gpt-4o-mini",
            "execution_mode": "live",
            "provider": "openai",
            "api_key": test_key,
            "capability": 3,
            "context_limit": 8000,
            "cost_per_1k_input": 0.01,
            "cost_per_1k_output": 0.01,
            "latency_ms": 300,
        },
    )
    assert post_resp.status_code == 201
    assert test_key not in post_resp.text

    # 2. Query workspace models
    get_models_resp = client.get("/api/workspace/models")
    assert test_key not in get_models_resp.text

    # 3. Query legacy bridge
    bridge_resp = client.get("/api/models")
    assert test_key not in bridge_resp.text

    # 4. Route request
    route_resp = client.post(
        "/route",
        json={
            "prompt": "Test secret containment",
            "max_latency_ms": 2000,
            "max_cost": 1.0,
            "min_capability": 1,
            "required_context": 100,
        },
    )
    assert route_resp.status_code == 200
    assert test_key not in route_resp.text

    # 5. History rows
    hist_resp = client.get("/api/history")
    assert test_key not in hist_resp.text

    # 6. Logger output (caplog)
    assert test_key not in caplog.text
