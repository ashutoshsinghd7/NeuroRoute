"""Tests for Milestone D: Empty workspaces, BYOK secret management, and live provider routing."""
import os
import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from src.gateway.db import init_workspace_schema
from src.gateway.live_call import rate_limiter
from src.gateway.logger import init_db
from src.gateway.main import app
from src.gateway.secrets_store import decrypt_secret, encrypt_secret, get_last4, is_configured


@pytest.fixture(autouse=True)
def setup_test_env(tmp_path, monkeypatch):
    """Set up temporary database and valid Fernet secret key."""
    db_file = str(tmp_path / "test_byok.db")
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


def test_secrets_store_encrypt_decrypt_and_last4():
    """Verify encryption, decryption, and last4 extraction."""
    assert is_configured() is True
    raw = "sk-proj-super-secret-12345678"
    encrypted = encrypt_secret(raw)
    assert encrypted != raw
    decrypted = decrypt_secret(encrypted)
    assert decrypted == raw
    assert get_last4(raw) == "5678"


def test_secrets_store_not_configured(monkeypatch):
    """When NEUROROUTE_SECRET_KEY is missing, is_configured() is False."""
    monkeypatch.delenv("NEUROROUTE_SECRET_KEY", raising=False)
    assert is_configured() is False


def test_empty_workspace_and_no_eligible_models_refusal(client):
    """A fresh workspace starts with 0 occupied slots and route returns NO_VALID_MODEL refusal."""
    resp = client.get("/api/workspace/models")
    assert resp.status_code == 200
    slots = resp.json()["slots"]
    assert len(slots) == 6
    assert all(s["empty"] is True for s in slots)

    route_resp = client.post(
        "/route",
        json={
            "prompt": "test prompt",
            "max_latency_ms": 2000,
            "max_cost": 1.0,
            "min_capability": 1,
        },
    )
    assert route_resp.status_code == 200
    route_data = route_resp.json()
    assert route_data["status"] == "NO_VALID_MODEL"
    assert "No eligible model. Add a model to a slot or load demo models." in route_data["message"]


def test_load_demo_models_fills_only_empty_slots(client):
    """POST /api/workspace/load-demo-models populates only empty slots and preserves occupied ones."""
    # First add a custom model in slot 1
    custom_model = {
        "model_name": "my-custom-model",
        "model_id": "custom-v1",
        "capability": 4,
        "context_limit": 16000,
        "cost_per_1k": 0.1,
        "latency_ms": 500,
        "failure_rate": 0.0,
        "provider": "simulated",
        "status": "up",
        "enabled": True,
    }
    add_resp = client.post("/api/workspace/models/1", json=custom_model)
    assert add_resp.status_code == 201

    # Load demo models
    demo_resp = client.post("/api/workspace/load-demo-models")
    assert demo_resp.status_code == 200
    slots = demo_resp.json()["slots"]

    # Slot 1 should still be custom model
    assert slots[0]["model"]["name"] == "my-custom-model"
    # Slots 2-6 should now be occupied by default models
    assert slots[1]["empty"] is False
    assert slots[1]["model"]["name"] == "mini-2"
    assert slots[5]["empty"] is False


def test_reset_workspace_empties_all_slots(client, load_demo):
    """POST /api/models/reset empties all slots."""
    load_demo(client)
    models_before = client.get("/api/workspace/models").json()["slots"]
    assert any(not s["empty"] for s in models_before)

    reset_resp = client.post("/api/models/reset")
    assert reset_resp.status_code == 200
    assert reset_resp.json() == []

    models_after = client.get("/api/workspace/models").json()["slots"]
    assert all(s["empty"] is True for s in models_after)


def test_byok_slot_validation_and_crud(client):
    """Validate provider allowlist, missing key rejection, and key encryption."""
    # 1. Invalid provider -> 400
    bad_prov = {
        "model_name": "bad-prov",
        "model_id": "m1",
        "execution_mode": "live",
        "provider": "untrusted_provider",
        "api_key": "some-key",
        "capability": 3,
        "context_limit": 8000,
        "cost_per_1k_input": 0.01,
        "cost_per_1k_output": 0.02,
        "latency_ms": 500,
    }
    resp_bad = client.post("/api/workspace/models/1", json=bad_prov)
    assert resp_bad.status_code == 400
    assert resp_bad.json()["error"]["code"] == "INVALID_PROVIDER"

    # 2. Missing key for live provider -> 400
    no_key = {
        "model_name": "no-key-model",
        "model_id": "gpt-4o-mini",
        "execution_mode": "live",
        "provider": "openai",
        "capability": 3,
        "context_limit": 8000,
        "cost_per_1k_input": 0.01,
        "cost_per_1k_output": 0.02,
        "latency_ms": 500,
    }
    resp_nokey = client.post("/api/workspace/models/1", json=no_key)
    assert resp_nokey.status_code == 400
    assert resp_nokey.json()["error"]["code"] == "API_KEY_REQUIRED"

    # 3. Valid BYOK model -> 201
    valid_byok = {
        "model_name": "my-openai",
        "model_id": "gpt-4o-mini",
        "execution_mode": "live",
        "provider": "openai",
        "api_key": "sk-real-secret-1234",
        "capability": 4,
        "context_limit": 16000,
        "cost_per_1k_input": 0.005,
        "cost_per_1k_output": 0.015,
        "latency_ms": 600,
    }
    resp_valid = client.post("/api/workspace/models/1", json=valid_byok)
    assert resp_valid.status_code == 201
    created = resp_valid.json()
    assert created["execution_mode"] == "live"
    assert created["has_key"] is True
    assert created["key_last4"] == "1234"
    # Cost per 1k is average: (0.005 + 0.015) / 2 = 0.01
    assert abs(created["cost_per_1k"] - 0.01) < 1e-6
    # Ensure plaintext key is NEVER exposed
    assert "api_key" not in created
    assert "api_key_encrypted" not in created


def test_byok_key_preservation_on_put(client):
    """PUT /models/{slot} preserves existing key if key is omitted and provider unchanged."""
    client.post(
        "/api/workspace/models/1",
        json={
            "model_name": "my-openai",
            "model_id": "gpt-4o-mini",
            "execution_mode": "live",
            "provider": "openai",
            "api_key": "sk-initial-key-7777",
            "capability": 4,
            "context_limit": 16000,
            "cost_per_1k_input": 0.01,
            "cost_per_1k_output": 0.02,
            "latency_ms": 600,
        },
    )

    # PUT without api_key to update capability
    put_payload = {
        "model_name": "my-openai-updated",
        "model_id": "gpt-4o-mini",
        "execution_mode": "live",
        "provider": "openai",
        "capability": 5,
        "context_limit": 32000,
        "cost_per_1k_input": 0.01,
        "cost_per_1k_output": 0.02,
        "latency_ms": 700,
    }
    put_resp = client.put("/api/workspace/models/1", json=put_payload)
    assert put_resp.status_code == 200
    updated = put_resp.json()
    assert updated["model_name"] == "my-openai-updated"
    assert updated["has_key"] is True
    assert updated["key_last4"] == "7777"


def test_api_key_redacted_in_validation_error(client):
    """When 422 occurs on a body with api_key, the raw key is redacted."""
    # Send invalid payload where capability is wrong type, with sensitive api_key
    invalid_payload = {
        "model_name": "test-model",
        "model_id": "gpt-4o-mini",
        "execution_mode": "live",
        "provider": "openai",
        "api_key": "sk-super-secret-leaked-key",
        "capability": "not-an-integer",
        "context_limit": 1000,
        "cost_per_1k": 0.1,
        "latency_ms": 500,
    }
    resp = client.post("/api/workspace/models/1", json=invalid_payload)
    assert resp.status_code == 422
    text = resp.text
    assert "sk-super-secret-leaked-key" not in text


def test_safety_limits_prompt_length(client):
    """Prompt longer than 8000 characters is rejected with 400 PROMPT_TOO_LONG."""
    long_prompt = "a" * 8001
    resp = client.post(
        "/route",
        json={
            "prompt": long_prompt,
            "max_latency_ms": 2000,
            "max_cost": 1.0,
            "min_capability": 1,
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "PROMPT_TOO_LONG"


def test_safety_limits_rate_limiter():
    """Rate limiter allows 20 calls and blocks the 21st call within the window."""
    rate_limiter.reset()
    ws_id = 999
    for _ in range(20):
        assert rate_limiter.check_and_record(ws_id) is True
    assert rate_limiter.check_and_record(ws_id) is False


def test_live_route_with_mock_provider(client, monkeypatch):
    """Routing to a live slot invokes provider, returns content, and attaches trace['usage']."""
    # Configure mock handler for httpx
    def mock_handler(request: httpx.Request):
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "Real-time AI response"}}],
                "usage": {
                    "prompt_tokens": 150,
                    "completion_tokens": 60,
                    "total_tokens": 210,
                },
            },
        )

    # Patch httpx.AsyncClient in src.providers to use MockTransport
    orig_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *args, **kwargs: orig_client(transport=httpx.MockTransport(mock_handler)),
    )

    # Add live model
    client.post(
        "/api/workspace/models/1",
        json={
            "model_name": "live-openai",
            "model_id": "gpt-4o-mini",
            "execution_mode": "live",
            "provider": "openai",
            "api_key": "sk-mock-key-1234",
            "capability": 4,
            "context_limit": 16000,
            "cost_per_1k_input": 0.01,
            "cost_per_1k_output": 0.03,
            "latency_ms": 300,
        },
    )

    req = {
        "prompt": "Test live routing",
        "max_latency_ms": 2000,
        "max_cost": 1.0,
        "min_capability": 1,
        "required_context": 100,
    }
    resp = client.post("/route", json=req)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "OK"
    assert data["chosen_model"] == "live-openai"
    assert data["answer"] == "Real-time AI response"

    # Check trace usage
    assert "usage" in data["trace"]
    usage = data["trace"]["usage"]
    assert set(usage.keys()) == {"input_tokens", "output_tokens", "actual_cost", "latency_ms"}
    assert usage["input_tokens"] == 150
    assert usage["output_tokens"] == 60
    assert abs(usage["actual_cost"] - 0.0033) < 1e-5
    assert abs(data["trace"]["cost"] - 0.0033) < 1e-5


def test_live_route_failover_on_provider_auth_error(client, monkeypatch):
    """When a live model gets 401, its status becomes 'down' and routing fails over to backup model."""
    def mock_handler(request: httpx.Request):
        return httpx.Response(401, json={"error": {"message": "Invalid key"}})

    orig_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *args, **kwargs: orig_client(transport=httpx.MockTransport(mock_handler)),
    )

    # Slot 1: Live model with invalid key (faster)
    client.post(
        "/api/workspace/models/1",
        json={
            "model_name": "failing-live",
            "model_id": "gpt-4o-mini",
            "execution_mode": "live",
            "provider": "openai",
            "api_key": "bad-key",
            "capability": 3,
            "context_limit": 8000,
            "cost_per_1k_input": 0.01,
            "cost_per_1k_output": 0.01,
            "latency_ms": 200,
        },
    )

    # Slot 2: Simulated backup model (slower)
    client.post(
        "/api/workspace/models/2",
        json={
            "model_name": "backup-simulated",
            "model_id": "backup-v1",
            "execution_mode": "simulated",
            "provider": "simulated",
            "capability": 3,
            "context_limit": 8000,
            "cost_per_1k": 0.02,
            "latency_ms": 500,
            "status": "up",
        },
    )

    req = {
        "prompt": "Test failover on auth error",
        "max_latency_ms": 2000,
        "max_cost": 1.0,
        "min_capability": 1,
        "required_context": 100,
        "preference": "fastest",
    }
    resp = client.post("/route", json=req)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "OK"
    assert data["chosen_model"] == "backup-simulated"

    # Check that failing-live status was marked down with 'invalid API key'
    slot1 = client.get("/api/workspace/models").json()["slots"][0]["model"]
    assert slot1["status"] == "down"
    assert slot1["status_reason"] == "invalid API key"


def test_byok_secret_key_missing_returns_503(client, monkeypatch):
    """When NEUROROUTE_SECRET_KEY is missing, adding live model returns 503 SECRET_KEY_MISSING."""
    monkeypatch.delenv("NEUROROUTE_SECRET_KEY", raising=False)
    resp = client.post(
        "/api/workspace/models/1",
        json={
            "model_name": "live-model",
            "model_id": "gpt-4o-mini",
            "execution_mode": "live",
            "provider": "openai",
            "api_key": "some-key",
            "capability": 3,
            "context_limit": 8000,
            "latency_ms": 300,
        },
    )
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "SECRET_KEY_MISSING"
    assert "Live mode needs NEUROROUTE_SECRET_KEY. See README." in resp.json()["error"]["message"]


def test_live_slot_test_ping_endpoint(client, monkeypatch):
    """POST /models/{slot}/test sends 1-token live ping test."""
    def mock_handler(request: httpx.Request):
        assert b'"max_tokens":1' in request.content
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "pong"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    orig_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *args, **kwargs: orig_client(transport=httpx.MockTransport(mock_handler)),
    )

    client.post(
        "/api/workspace/models/1",
        json={
            "model_name": "ping-live",
            "model_id": "gpt-4o-mini",
            "execution_mode": "live",
            "provider": "openai",
            "api_key": "sk-ping-key",
            "capability": 3,
            "context_limit": 8000,
            "cost_per_1k_input": 0.01,
            "cost_per_1k_output": 0.01,
            "latency_ms": 300,
        },
    )

    test_resp = client.post("/api/workspace/models/1/test")
    assert test_resp.status_code == 200
    res = test_resp.json()
    assert res["success"] is True
    assert "Live ping succeeded" in res["message"]


def test_workspaces_isolated_keys(client):
    """Workspace A and Workspace B keys and slots are completely isolated."""
    client_a = TestClient(app)
    client_b = TestClient(app)

    # Client A creates live slot
    client_a.post(
        "/api/workspace/models/1",
        json={
            "model_name": "client-a-model",
            "model_id": "gpt-4o-mini",
            "execution_mode": "live",
            "provider": "openai",
            "api_key": "sk-client-a-key-1111",
            "capability": 4,
            "context_limit": 8000,
            "cost_per_1k_input": 0.01,
            "cost_per_1k_output": 0.01,
            "latency_ms": 300,
        },
    )

    # Client B workspace should have slot 1 empty
    slots_b = client_b.get("/api/workspace/models").json()["slots"]
    assert slots_b[0]["empty"] is True
    assert slots_b[0]["model"] is None
