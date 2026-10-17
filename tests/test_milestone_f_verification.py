"""Milestone F Verification & Regression Tests.
Covers:
- Type annotations introspection (prevent NameError on typing hints like Optional/Any)
- Database schema slot number CHECK constraints (slot 0 and 7+ prohibited)
- Workspace isolation and duplicate model handling
- CSRF defense verification on state-changing endpoints
- Graceful handling when NEUROROUTE_SECRET_KEY is missing
"""
import inspect
import sqlite3
import typing
import pytest
from fastapi.testclient import TestClient

from src.gateway.db import get_conn, init_workspace_schema
from src.gateway.main import app, route
from src.gateway.session import get_optional_workspace, get_workspace_id
from src.gateway.workspace_api import get_workspace_models, add_workspace_model, replace_workspace_model
from src.gateway.workspace_repo import create_workspace


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path, monkeypatch):
    """Isolate database to a fresh temporary SQLite file for each test."""
    db_file = str(tmp_path / "test_neuro_f.db")
    monkeypatch.setenv("NEUROROUTE_DB", db_file)
    init_workspace_schema()
    yield db_file


@pytest.fixture
def client():
    return TestClient(app)


def test_type_hints_introspection():
    """Verify that type hints on all endpoints and dependencies can be resolved without NameError."""
    # This was a bug in session.py (missing Optional) and main.py (missing Any)
    hints_session = typing.get_type_hints(get_optional_workspace)
    assert hints_session["return"] == typing.Optional[int]

    hints_main = typing.get_type_hints(route)
    assert "workspace_id" in hints_main

    for fn in [get_workspace_id, get_workspace_models, add_workspace_model, replace_workspace_model]:
        hints = typing.get_type_hints(fn)
        assert isinstance(hints, dict)


def test_db_slot_number_check_constraint(setup_test_db):
    """Verify SQLite CHECK constraint rejects slot numbers < 1 and > 6."""
    ws_id = create_workspace("testhash123")
    conn = get_conn()

    # Test slot 0 rejected
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute(
                """INSERT INTO model_slots (workspace_id, slot_number, model_name, model_id, capability,
                   context_limit, cost_per_1k, latency_ms, created_at, updated_at)
                   VALUES (?, ?, 'm0', 'm0-id', 1, 1000, 1.0, 100, '2026-01-01', '2026-01-01')""",
                (ws_id, 0),
            )

    # Test slot 7 rejected
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute(
                """INSERT INTO model_slots (workspace_id, slot_number, model_name, model_id, capability,
                   context_limit, cost_per_1k, latency_ms, created_at, updated_at)
                   VALUES (?, ?, 'm7', 'm7-id', 1, 1000, 1.0, 100, '2026-01-01', '2026-01-01')""",
                (ws_id, 7),
            )

    # Test slot 1 accepted
    with conn:
        conn.execute(
            """INSERT INTO model_slots (workspace_id, slot_number, model_name, model_id, capability,
               context_limit, cost_per_1k, latency_ms, created_at, updated_at)
               VALUES (?, ?, 'm1', 'm1-id', 1, 1000, 1.0, 100, '2026-01-01', '2026-01-01')""",
            (ws_id, 1),
        )

    # Test duplicate model_name rejected by UNIQUE(workspace_id, model_name)
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute(
                """INSERT INTO model_slots (workspace_id, slot_number, model_name, model_id, capability,
                   context_limit, cost_per_1k, latency_ms, created_at, updated_at)
                   VALUES (?, ?, 'm1', 'm2-id', 1, 1000, 1.0, 100, '2026-01-01', '2026-01-01')""",
                (ws_id, 2),
            )

    conn.close()


def test_secret_key_missing_safe_handling(client, monkeypatch):
    """Verify that missing NEUROROUTE_SECRET_KEY safely returns 503 with SECRET_KEY_MISSING."""
    monkeypatch.delenv("NEUROROUTE_SECRET_KEY", raising=False)
    # Establish workspace session
    client.get("/api/workspace/models")

    # Attempt to save a model with an api_key when encryption key is missing
    payload = {
        "model_name": "custom-gpt4",
        "model_id": "gpt-4o",
        "capability": 4,
        "context_limit": 8192,
        "provider": "openai",
        "execution_mode": "live",
        "cost_per_1k": 5.0,
        "latency_ms": 400,
        "api_key": "sk-proj-testkey123",
    }
    resp = client.post("/api/workspace/models/1", json=payload)
    assert resp.status_code == 503
    data = resp.json()
    assert data["error"]["code"] == "SECRET_KEY_MISSING"
    assert "Live mode needs NEUROROUTE_SECRET_KEY" in data["error"]["message"]
    # Ensure raw API key was not leaked in error
    assert "sk-proj-testkey123" not in resp.text


def test_csrf_guard_all_state_changing_methods(client):
    """Verify CSRF checks prevent cross-origin POST, PUT, DELETE requests."""
    foreign_origin = {"Origin": "https://malicious-site.com"}

    # POST load demo models
    r_post_demo = client.post("/api/workspace/models/load-demo-models", headers=foreign_origin)
    assert r_post_demo.status_code == 403
    assert r_post_demo.json()["error"]["code"] == "CSRF_FAILED"

    # POST route
    r_post_route = client.post("/route", json={"prompt": "hello"}, headers=foreign_origin)
    assert r_post_route.status_code == 403
    assert r_post_route.json()["error"]["code"] == "CSRF_FAILED"

    # PUT slot
    r_put = client.put("/api/workspace/models/1", json={"enabled": False}, headers=foreign_origin)
    assert r_put.status_code == 403
    assert r_put.json()["error"]["code"] == "CSRF_FAILED"

    # DELETE slot
    r_del = client.delete("/api/workspace/models/1", headers=foreign_origin)
    assert r_del.status_code == 403
    assert r_del.json()["error"]["code"] == "CSRF_FAILED"
