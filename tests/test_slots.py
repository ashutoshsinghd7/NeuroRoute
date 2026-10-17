"""Tests for Milestone A: Workspace database, guest sessions, and slot APIs."""
import hashlib
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
import pytest
from fastapi.testclient import TestClient

from src.gateway.db import get_conn, init_workspace_schema
from src.gateway.main import app
from src.gateway.workspace_repo import get_workspace_by_token_hash


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path, monkeypatch):
    """Isolate database to a fresh temporary SQLite file for each test."""
    db_file = str(tmp_path / "test_neuro.db")
    monkeypatch.setenv("NEUROROUTE_DB", db_file)
    init_workspace_schema()
    yield db_file


@pytest.fixture
def client():
    """FastAPI TestClient fixture."""
    return TestClient(app)


def test_new_session_workspace_and_six_defaults(client, load_demo):
    """New session creates a workspace, returns 6 defaults after load-demo-models, and reuses session cookie."""
    load_demo(client)
    resp = client.get("/api/workspace/models")
    assert resp.status_code == 200
    data = resp.json()
    assert "slots" in data
    slots = data["slots"]
    assert len(slots) == 6

    # Verify slots 1-6 are occupied by default models
    expected_names = ["nano-1", "mini-2", "pro-3", "ultra-4", "max-5", "local-llama"]
    for idx, slot_data in enumerate(slots, start=1):
        assert slot_data["slot"] == idx
        assert slot_data["empty"] is False
        assert slot_data["model"]["name"] == expected_names[idx - 1]
        assert slot_data["model"]["enabled"] is True
        assert slot_data["model"]["status"] == "up"

    # Verify cookie was set
    cookie = client.cookies.get("nr_session")
    assert cookie is not None

    # Next request reuses workspace
    resp2 = client.get("/api/workspace/models")
    assert resp2.status_code == 200
    assert client.cookies.get("nr_session") == cookie


def test_two_clients_isolated(client, load_demo):
    """Two different clients receive different workspaces with isolated state."""
    client1 = TestClient(app)
    client2 = TestClient(app)
    load_demo(client1)
    load_demo(client2)

    # Client 1 removes model in slot 1
    resp1_init = client1.get("/api/workspace/models")
    assert resp1_init.status_code == 200
    del_resp = client1.delete("/api/workspace/models/1")
    assert del_resp.status_code == 200

    slots1 = client1.get("/api/workspace/models").json()["slots"]
    assert slots1[0]["empty"] is True
    assert slots1[0]["model"] is None

    # Client 2 should still have slot 1 occupied
    slots2 = client2.get("/api/workspace/models").json()["slots"]
    assert slots2[0]["empty"] is False
    assert slots2[0]["model"]["name"] == "nano-1"


def test_forged_workspace_id_ignored(client, load_demo):
    """Server ignores any client-supplied workspace_id in query, header, or body."""
    # First get a valid session and load demo models
    load_demo(client)
    resp = client.get("/api/workspace/models")
    assert resp.status_code == 200

    # Attempt to supply a forged workspace_id
    resp_query = client.get("/api/workspace/models?workspace_id=999999", headers={"X-Workspace-Id": "999999"})
    assert resp_query.status_code == 200
    slots = resp_query.json()["slots"]
    assert len(slots) == 6
    assert slots[0]["model"]["name"] == "nano-1"


def test_slot_crud_and_validation(client, load_demo):
    """Test POST occupied -> 409, DELETE -> empty, POST empty -> 201, PUT replace, and invalid slot 0/7 -> 400."""
    # Establish session and populate demo models
    load_demo(client)

    new_model = {
        "model_name": "custom-gpt",
        "model_id": "custom-gpt-v1",
        "capability": 4,
        "context_limit": 32000,
        "cost_per_1k": 0.25,
        "latency_ms": 800,
        "failure_rate": 0.05,
        "provider": "simulated",
        "status": "up",
        "enabled": True,
    }

    # 1. POST to occupied slot 1 -> 409
    post_occ = client.post("/api/workspace/models/1", json=new_model)
    assert post_occ.status_code == 409
    assert post_occ.json()["error"]["code"] == "SLOT_OCCUPIED"

    # 2. DELETE slot 1 -> 200
    del_resp = client.delete("/api/workspace/models/1")
    assert del_resp.status_code == 200

    # 3. POST to now-empty slot 1 -> 201
    post_empty = client.post("/api/workspace/models/1", json=new_model)
    assert post_empty.status_code == 201
    created = post_empty.json()
    assert created["model_name"] == "custom-gpt"
    assert created["slot_number"] == 1

    # 4. PUT replaces occupied slot 1 (slot number remains 1)
    replace_model = {
        "model_name": "custom-claude",
        "model_id": "custom-claude-v2",
        "capability": 5,
        "context_limit": 100000,
        "cost_per_1k": 0.50,
        "latency_ms": 1100,
        "failure_rate": 0.01,
        "provider": "simulated",
        "status": "up",
        "enabled": True,
    }
    put_resp = client.put("/api/workspace/models/1", json=replace_model)
    assert put_resp.status_code == 200
    updated = put_resp.json()
    assert updated["model_name"] == "custom-claude"
    assert updated["slot_number"] == 1

    # 5. Invalid slots (0 and 7) return 400
    for invalid_slot in (0, 7):
        assert client.post(f"/api/workspace/models/{invalid_slot}", json=new_model).status_code == 400
        assert client.put(f"/api/workspace/models/{invalid_slot}", json=replace_model).status_code == 400
        assert client.patch(f"/api/workspace/models/{invalid_slot}", json={"status": "down"}).status_code == 400
        assert client.delete(f"/api/workspace/models/{invalid_slot}").status_code == 400
        assert client.patch(f"/api/workspace/models/{invalid_slot}/toggle").status_code == 400
        assert client.post(f"/api/workspace/models/{invalid_slot}/test").status_code == 400


def test_toggle_and_patch(client, load_demo):
    """Test toggle enabled on/off and PATCH latency/failure/status."""
    load_demo(client)

    # Toggle slot 2 enabled from True to False
    toggle1 = client.patch("/api/workspace/models/2/toggle")
    assert toggle1.status_code == 200
    assert toggle1.json()["enabled"] is False

    # Toggle slot 2 enabled back to True
    toggle2 = client.patch("/api/workspace/models/2/toggle")
    assert toggle2.status_code == 200
    assert toggle2.json()["enabled"] is True

    # PATCH latency, failure_rate, and status
    patch_resp = client.patch("/api/workspace/models/2", json={
        "latency_ms": 1500,
        "failure_rate": 0.25,
        "status": "down",
    })
    assert patch_resp.status_code == 200
    patched = patch_resp.json()
    assert patched["latency_ms"] == 1500
    assert patched["failure_rate"] == 0.25
    assert patched["status"] == "down"


def test_seventh_slot_impossible(setup_test_db, client):
    """Database CHECK constraint makes a 7th slot impossible."""
    client.get("/api/workspace/models")
    conn = get_conn()
    ws_row = conn.execute("SELECT id FROM workspaces LIMIT 1").fetchone()
    ws_id = ws_row["id"]

    now = datetime.now(timezone.utc).isoformat()
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute(
                """
                INSERT INTO model_slots(
                    workspace_id, slot_number, model_name, provider, model_id,
                    capability, context_limit, cost_per_1k, latency_ms,
                    failure_rate, status, enabled, created_at, updated_at
                ) VALUES (?, 7, 'slot7', 'simulated', 'slot7', 3, 1000, 0.1, 500, 0, 'up', 1, ?, ?)
                """,
                (ws_id, now, now),
            )
    conn.close()


def test_duplicate_model_name_rejected(client, load_demo):
    """Duplicate model_name in the same workspace returns 409."""
    load_demo(client)
    client.get("/api/workspace/models")

    # Delete slot 2
    client.delete("/api/workspace/models/2")

    # Try to add model in slot 2 with name 'nano-1' which is already in slot 1
    dup_model = {
        "model_name": "nano-1",
        "model_id": "nano-duplicate",
        "capability": 2,
        "context_limit": 8000,
        "cost_per_1k": 0.05,
        "latency_ms": 400,
        "failure_rate": 0.0,
        "provider": "simulated",
        "status": "up",
        "enabled": True,
    }
    resp = client.post("/api/workspace/models/2", json=dup_model)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "DUPLICATE_MODEL_NAME"


def test_concurrent_initialization(setup_test_db):
    """Concurrent requests from distinct clients initialize workspaces without integrity errors."""
    errors = []

    def run_client():
        try:
            c = TestClient(app)
            r = c.get("/api/workspace/models")
            if r.status_code != 200 or len(r.json()["slots"]) != 6:
                errors.append(f"Unexpected response: {r.status_code} {r.text}")
        except Exception as e:
            errors.append(str(e))

    threads = [threading.Thread(target=run_client) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Concurrent initialization had errors: {errors}"


def test_unknown_or_expired_cookie_creates_new_workspace(setup_test_db):
    """Unknown or expired cookie triggers creation of a new isolated workspace."""
    # 1. Unknown cookie token passed by browser
    c1 = TestClient(app)
    resp = c1.get("/api/workspace/models", headers={"Cookie": "nr_session=completely_unknown_token_xyz"})
    assert resp.status_code == 200
    new_cookie = resp.cookies.get("nr_session")
    assert new_cookie is not None
    assert new_cookie != "completely_unknown_token_xyz"

    # 2. Expired cookie token
    conn = get_conn()
    with conn:
        past = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        conn.execute("UPDATE workspaces SET expires_at = ?", (past,))
    conn.close()

    # Pass the now-expired cookie
    c2 = TestClient(app)
    resp_after_expiry = c2.get("/api/workspace/models", headers={"Cookie": f"nr_session={new_cookie}"})
    assert resp_after_expiry.status_code == 200
    after_cookie = resp_after_expiry.cookies.get("nr_session")
    assert after_cookie is not None
    assert after_cookie != new_cookie


def test_model_test_endpoint_writes_db_and_no_network(client, load_demo):
    """Test endpoint executes simulated test, writes to model_test_runs, and returns result."""
    load_demo(client)
    client.get("/api/workspace/models")

    resp = client.post("/api/workspace/models/1/test")
    assert resp.status_code == 200
    data = resp.json()
    assert "success" in data
    assert "latency_ms" in data
    assert "message" in data
    assert data["latency_ms"] == 300  # nano-1 default latency

    conn = get_conn()
    runs = conn.execute("SELECT * FROM model_test_runs WHERE slot_number = 1").fetchall()
    assert len(runs) >= 1
    assert runs[0]["latency_ms"] == 300
    conn.close()


def test_cookie_security_and_token_hash_storage(client):
    """Verify cookie security flags and that raw session token is NEVER stored in the database."""
    resp = client.get("/api/workspace/models")
    assert resp.status_code == 200

    set_cookie_header = resp.headers.get("set-cookie", "")
    assert "httponly" in set_cookie_header.lower()
    assert "samesite=lax" in set_cookie_header.lower()

    raw_token = client.cookies.get("nr_session")
    assert raw_token is not None

    conn = get_conn()
    # Ensure raw token is NOT in the database
    raw_match = conn.execute(
        "SELECT id FROM workspaces WHERE session_token_hash = ?", (raw_token,)
    ).fetchone()
    assert raw_match is None

    # Ensure hash of the token IS in the database
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    hash_match = conn.execute(
        "SELECT id FROM workspaces WHERE session_token_hash = ?", (token_hash,)
    ).fetchone()
    assert hash_match is not None
    conn.close()


def test_cors_and_csrf_origin_protection(client):
    """CORS response to foreign Origin has no Access-Control-Allow-Origin, and CSRF rejects foreign Origin."""
    # 1. Foreign origin request has no Access-Control-Allow-Origin header
    resp = client.get("/api/workspace/models", headers={"Origin": "https://attacker.example.com"})
    assert resp.status_code == 200
    assert "access-control-allow-origin" not in resp.headers

    # 2. State-changing request with foreign Origin is rejected by CSRF guard with 403
    csrf_resp = client.post(
        "/api/workspace/models/1/test",
        headers={"Origin": "https://attacker.example.com"},
    )
    assert csrf_resp.status_code == 403
    assert csrf_resp.json()["error"]["code"] == "CSRF_FAILED"
