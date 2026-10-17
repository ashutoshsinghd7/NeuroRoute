"""Tests for Milestone B: Connecting workspace slots to routing, history, and legacy endpoints."""
import asyncio
import copy
import os
import sqlite3
import pytest
from fastapi.testclient import TestClient

from src.gateway.db import get_conn, init_workspace_schema
from src.gateway.logger import init_db
from src.gateway.main import app
from src.gateway.router import route_request
from src.gateway.schemas import RouteRequest, load_registry
from src.gateway.workspace_repo import load_routing_models


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path, monkeypatch):
    """Isolate database to a fresh temporary SQLite file for each test."""
    db_file = str(tmp_path / "test_routing.db")
    monkeypatch.setenv("NEUROROUTE_DB", db_file)
    init_db()
    init_workspace_schema()
    yield db_file


@pytest.fixture
def client():
    """FastAPI TestClient fixture."""
    return TestClient(app)


def test_route_with_session_routes_workspace_models(client, load_demo):
    """POST /route with a session routes only among workspace models and flags source='workspace'."""
    # Initialize workspace session and load demo models
    load_demo(client)

    req = {
        "prompt": "Hello world routing test",
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
    assert data["chosen_model"] == "nano-1"
    assert data["trace"]["source"] == "workspace"


def test_delete_chosen_model_slot_excludes_from_routing(client, load_demo):
    """Deleting a chosen model's slot ensures it is never chosen or considered in subsequent routing."""
    # Initialize session and load demo models
    load_demo(client)

    req = {
        "prompt": "Hello world routing test",
        "max_latency_ms": 2000,
        "max_cost": 1.0,
        "min_capability": 1,
        "required_context": 100,
        "preference": "fastest",
    }
    # Initial route selects nano-1 in slot 1
    resp1 = client.post("/route", json=req)
    assert resp1.json()["chosen_model"] == "nano-1"

    # Delete slot 1
    del_resp = client.delete("/api/workspace/models/1")
    assert del_resp.status_code == 200

    # Next route request must not pick or even consider nano-1
    resp2 = client.post("/route", json=req)
    data2 = resp2.json()
    assert data2["status"] == "OK"
    assert data2["chosen_model"] != "nano-1"
    considered_names = [c["model"] for c in data2["trace"]["considered"]]
    assert "nano-1" not in considered_names


def test_toggle_cheapest_model_off_marks_disabled(client, load_demo):
    """Toggling the cheapest model off marks it as 'model is disabled', never attempted."""
    load_demo(client)

    # Toggle slot 6 (local-llama, cost=0.0, cheapest) off
    toggle_resp = client.patch("/api/workspace/models/6/toggle")
    assert toggle_resp.status_code == 200
    assert toggle_resp.json()["enabled"] is False

    req = {
        "prompt": "Hello world routing test",
        "max_latency_ms": 2000,
        "max_cost": 1.0,
        "min_capability": 1,
        "required_context": 100,
        "preference": "cheapest",
    }
    resp = client.post("/route", json=req)
    data = resp.json()
    assert data["status"] == "OK"
    assert data["chosen_model"] != "local-llama"

    # Check considered trace
    c_llama = next(c for c in data["trace"]["considered"] if c["model"] == "local-llama")
    assert c_llama["valid"] is False
    assert c_llama["reason"] == "model is disabled"

    # Check local-llama was never in attempts
    attempted_names = [a["model"] for a in data["trace"]["attempts"]]
    assert "local-llama" not in attempted_names


def test_all_models_disabled_or_removed_returns_no_valid_model(client, load_demo):
    """When all models are disabled or all slots removed, routing returns NO_VALID_MODEL with zero attempts."""
    load_demo(client)

    # Disable all 6 slots
    for slot in range(1, 7):
        client.patch(f"/api/workspace/models/{slot}/toggle")

    req = {
        "prompt": "Hello world routing test",
        "max_latency_ms": 5000,
        "max_cost": 5.0,
        "min_capability": 1,
        "required_context": 100,
        "preference": "cheapest",
    }
    resp = client.post("/route", json=req)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "NO_VALID_MODEL"
    assert data["chosen_model"] is None
    assert data["trace"]["attempts"] == []


def test_two_clients_routing_isolated(client, load_demo):
    """Client A mutations do not affect Client B routing decisions."""
    client_a = TestClient(app)
    client_b = TestClient(app)

    # Initialize both with demo models
    load_demo(client_a)
    load_demo(client_b)

    # Client A deletes slot 1 (nano-1) and slot 2 (mini-2)
    client_a.delete("/api/workspace/models/1")
    client_a.delete("/api/workspace/models/2")

    req = {
        "prompt": "Hello world routing test",
        "max_latency_ms": 2000,
        "max_cost": 1.0,
        "min_capability": 1,
        "required_context": 100,
        "preference": "fastest",
    }

    resp_a = client_a.post("/route", json=req)
    assert resp_a.json()["chosen_model"] not in ("nano-1", "mini-2")

    # Client B should still choose nano-1
    resp_b = client_b.post("/route", json=req)
    assert resp_b.json()["chosen_model"] == "nano-1"


def test_unknown_or_expired_cookie_routes_new_workspace_never_legacy():
    """An unknown/expired session cookie spawns a new workspace and sets source='workspace', never 'legacy'."""
    c = TestClient(app)
    req = {
        "prompt": "Hello world routing test",
        "max_latency_ms": 2000,
        "max_cost": 1.0,
        "min_capability": 1,
        "required_context": 100,
        "preference": "cheapest",
    }
    resp = c.post(
        "/route",
        json=req,
        headers={"Cookie": "nr_session=unknown_nonexistent_token_12345"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["trace"]["source"] == "workspace"
    # Ensure new cookie was set on the response
    new_cookie = resp.cookies.get("nr_session")
    assert new_cookie is not None
    assert new_cookie != "unknown_nonexistent_token_12345"


def test_no_cookie_routes_legacy_and_registry_unmodified():
    """A caller with no session cookie routes in legacy mode; static registry file is unmodified."""
    src_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    reg_path = os.path.join(src_dir, "src", "models", "registry.json")
    with open(reg_path, "rb") as f:
        bytes_before = f.read()

    c = TestClient(app)
    req = {
        "prompt": "Hello world legacy route",
        "max_latency_ms": 2000,
        "max_cost": 1.0,
        "min_capability": 1,
        "required_context": 100,
        "preference": "fastest",
    }
    resp = c.post("/route", json=req)
    assert resp.status_code == 200
    data = resp.json()
    assert data["trace"]["source"] == "legacy"
    assert data["chosen_model"] == "nano-1"

    with open(reg_path, "rb") as f:
        bytes_after = f.read()
    assert bytes_before == bytes_after


def test_history_isolated_and_no_cookie_returns_empty(load_demo):
    """Client A sees only A's requests, Client B only B's, and no-cookie request returns []."""
    client_a = TestClient(app)
    client_b = TestClient(app)
    client_no_cookie = TestClient(app)

    # Initialize workspaces with demo models
    load_demo(client_a)
    load_demo(client_b)

    req_a = {
        "prompt": "Prompt for Client A",
        "max_latency_ms": 2000,
        "max_cost": 1.0,
        "min_capability": 1,
        "required_context": 100,
    }
    req_b = {
        "prompt": "Prompt for Client B",
        "max_latency_ms": 2000,
        "max_cost": 1.0,
        "min_capability": 1,
        "required_context": 100,
    }

    client_a.post("/route", json=req_a)
    client_b.post("/route", json=req_b)

    hist_a = client_a.get("/api/history").json()
    assert len(hist_a) == 1
    assert "Prompt for Client A" in hist_a[0]["prompt_preview"]

    hist_b = client_b.get("/api/history").json()
    assert len(hist_b) == 1
    assert "Prompt for Client B" in hist_b[0]["prompt_preview"]

    hist_no_cookie = client_no_cookie.get("/api/history").json()
    assert hist_no_cookie == []


def test_old_history_rows_survive_migration(tmp_path, monkeypatch):
    """Old requests table without workspace_id column is migrated and existing rows survive intact."""
    mig_db = str(tmp_path / "migration_test.db")
    monkeypatch.setenv("NEUROROUTE_DB", mig_db)

    # Build DB with legacy schema without workspace_id
    conn = sqlite3.connect(mig_db)
    with conn:
        conn.execute(
            """
            CREATE TABLE requests(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                time TEXT,
                prompt_preview TEXT,
                constraints TEXT,
                estimated_level INTEGER,
                confidence REAL,
                chosen_model TEXT,
                status TEXT,
                total_latency INTEGER,
                cost REAL,
                attempts_json TEXT,
                rejected_json TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO requests(time, prompt_preview, constraints, estimated_level, confidence, chosen_model, status, total_latency, cost, attempts_json, rejected_json)
            VALUES ('10:00:00', 'legacy prompt before migration', '{}', 1, 1.0, 'nano-1', 'OK', 150, 0.01, '[]', '[]')
            """
        )
    conn.close()

    # Run init_workspace_schema to execute migration
    init_workspace_schema()

    conn = get_conn()
    col_names = [r["name"] for r in conn.execute("PRAGMA table_info(requests)").fetchall()]
    assert "workspace_id" in col_names

    old_row = conn.execute("SELECT * FROM requests WHERE id = 1").fetchone()
    assert old_row is not None
    assert old_row["prompt_preview"] == "legacy prompt before migration"
    assert old_row["workspace_id"] is None
    conn.close()


def test_legacy_patch_and_reset_affect_only_caller_workspace(load_demo):
    """Legacy PATCH /api/models/{name} and POST /api/models/reset are scoped to the caller's workspace."""
    client_a = TestClient(app)
    client_b = TestClient(app)

    # Initialize workspaces with demo models
    load_demo(client_a)
    load_demo(client_b)

    # Client A patches nano-1 latency to 8888
    patch_resp = client_a.patch("/api/models/nano-1", json={"latency_ms": 8888})
    assert patch_resp.status_code == 200
    assert patch_resp.json()["latency_ms"] == 8888

    # Client B should still have nano-1 default latency (300)
    models_b = client_b.get("/api/models").json()
    nano_b = next(m for m in models_b if m["name"] == "nano-1")
    assert nano_b["latency_ms"] == 300

    # Client A resets fleet (empties the workspace)
    reset_resp = client_a.post("/api/models/reset")
    assert reset_resp.status_code == 200
    assert reset_resp.json() == []

    # Client B still has its models intact
    models_b_after = client_b.get("/api/models").json()
    assert len(models_b_after) == 6


def test_failover_in_workspace_routing_after_failure_rate_patch(client, load_demo):
    """Setting failure_rate=1.0 via PATCH triggers failover to next valid model, showing both attempts."""
    load_demo(client)

    # Set nano-1 failure rate to 1.0 (guaranteed failure)
    client.patch("/api/models/nano-1", json={"failure_rate": 1.0})

    req = {
        "prompt": "Hello failover test",
        "max_latency_ms": 3000,
        "max_cost": 1.0,
        "min_capability": 1,
        "required_context": 100,
        "preference": "fastest",
    }
    resp = client.post("/route", json=req)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "OK"
    assert data["chosen_model"] != "nano-1"

    attempts = data["trace"]["attempts"]
    assert len(attempts) >= 2
    assert attempts[0]["model"] == "nano-1"
    assert attempts[0]["status"] == "error"
    assert attempts[1]["status"] == "success"


def test_constraints_enforced_for_workspace_models(client, load_demo):
    """Hard constraints (cost, latency, context) remain strictly enforced for workspace models."""
    load_demo(client)

    # Max latency 200 ms is lower than nano-1 (300 ms) and all other fleet models
    req = {
        "prompt": "Constraint enforcement test",
        "max_latency_ms": 200,
        "max_cost": 1.0,
        "min_capability": 1,
        "required_context": 100,
    }
    resp = client.post("/route", json=req)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "NO_VALID_MODEL"
    assert data["chosen_model"] is None
    assert data["trace"]["attempts"] == []


def test_disabled_or_down_model_is_never_called(client, load_demo):
    """An injected call function verifies that disabled and down models are never called."""
    load_demo(client)

    # Set nano-1 down, and mini-2 disabled
    client.patch("/api/models/nano-1", json={"status": "down"})
    client.patch("/api/workspace/models/2/toggle")

    # Retrieve active workspace models
    ws_models = client.get("/api/models").json()

    called = []
    async def recording_call(model, prompt):
        called.append(model["name"])
        return f"Response from {model['name']}"

    req = RouteRequest(
        prompt="Test disabled and down models are not called",
        max_latency_ms=4000,
        max_cost=1.0,
        min_capability=1,
        required_context=100,
        preference="cheapest",
    )

    out = asyncio.run(route_request(req, ws_models, call=recording_call))
    assert out["status"] == "OK"
    assert "nano-1" not in called
    assert "mini-2" not in called

