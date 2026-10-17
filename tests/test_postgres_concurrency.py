"""Tests for Milestone G7.4: PostgreSQL Reliability, Concurrency, and Recovery.

Covers:
1. Concurrent creation of models with identical names in the same workspace (uniqueness enforcement).
2. Concurrent creation of models on the same slot (slot uniqueness collision).
3. Concurrent creation of distinct models across slots (parallel non-conflicting writes).
4. Correct HTTP status codes and structured error payloads for conflicting operations (409 DUPLICATE_MODEL_NAME, 409 SLOT_OCCUPIED).
5. Consistent slot limits and validation under concurrency (1-6 bounds).
6. Transaction rollback after failed writes (no partial or corrupted state).
7. Connection pool recovery from closed/unusable connections.
8. Connection transaction state verification (failed transactions return to IDLE, never left in INERROR).
9. Connection pool exhaustion and timeout behavior.
10. Connection error propagation with sanitized credentials.
11. Subsequent healthy requests succeed after transient failures.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
import os
import secrets
from typing import Generator
import pytest
from cryptography.fernet import Fernet
from starlette.testclient import TestClient

import psycopg
from psycopg.rows import dict_row
import psycopg_pool

from src.gateway.db import (
    DB_INTEGRITY_ERRORS,
    DatabaseConnectionError,
    close_pg_pool,
    get_connection,
    get_pg_pool,
    mask_database_url,
    reset_pg_pool,
)
from src.gateway.main import app
from src.gateway.session import hash_token
from src.gateway.workspace_repo import (
    DuplicateModelNameError,
    SlotOccupiedError,
    add_model,
    create_workspace,
    get_slot,
    list_slots,
)


def _get_postgres_admin_credentials() -> tuple[str, str]:
    """Retrieve postgres port and password safely from pgpass.conf without leaking."""
    p2 = os.path.join(os.environ.get("APPDATA", ""), "postgresql", "pgpass.conf")
    if not os.path.exists(p2):
        pytest.skip("PostgreSQL pgpass.conf not found for local test database")
    with open(p2, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                parts = line.split(":")
                if len(parts) >= 5:
                    return parts[1], parts[4]  # port, password
    pytest.skip("No valid PostgreSQL credentials found in pgpass.conf")


@pytest.fixture(scope="module")
def pg_concurrency_db() -> Generator[str, None, None]:
    """Provision a dedicated, isolated PostgreSQL test database (neuroroute_concurrency_test),
    apply the schema migrations, yield the test DATABASE_URL, and clean it up completely on exit.
    Strictly avoids touching neuroroute.db or neuroroute_test.
    """
    port, pwd = _get_postgres_admin_credentials()
    admin_url = f"postgresql://postgres:{pwd}@127.0.0.1:{port}/postgres"
    test_db_name = "neuroroute_concurrency_test"
    test_db_url = f"postgresql://postgres:{pwd}@127.0.0.1:{port}/{test_db_name}"

    # 1. Create clean isolated test database
    try:
        with psycopg.connect(admin_url, autocommit=True) as conn:
            conn.execute(f"DROP DATABASE IF EXISTS {test_db_name};")
            conn.execute(f"CREATE DATABASE {test_db_name};")
    except Exception as e:
        pytest.skip(f"Could not connect to PostgreSQL to create test database: {e}")

    # 2. Apply initial PostgreSQL schema
    migrations_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "migrations",
        "postgres",
    )
    schema_file = os.path.join(migrations_dir, "001_initial_schema.sql")
    with open(schema_file, "r", encoding="utf-8") as f:
        schema_sql = f.read()

    with psycopg.connect(test_db_url, autocommit=True) as conn:
        conn.execute(schema_sql)

    # 3. Configure environment
    orig_env = os.environ.copy()
    os.environ["DATABASE_URL"] = test_db_url
    os.environ["NEUROROUTE_SECRET_KEY"] = Fernet.generate_key().decode()
    os.environ.pop("NEUROROUTE_DB", None)
    os.environ.pop("VERCEL", None)
    os.environ.pop("ENVIRONMENT", None)

    reset_pg_pool()

    yield test_db_url

    # 4. Teardown
    close_pg_pool()
    os.environ.clear()
    os.environ.update(orig_env)

    try:
        with psycopg.connect(admin_url, autocommit=True) as conn:
            # Terminate active backend connections to test db before dropping
            conn.execute(
                f"""
                SELECT pg_terminate_backend(pid)
                FROM pg_stat_activity
                WHERE datname = '{test_db_name}' AND pid <> pg_backend_pid();
                """
            )
            conn.execute(f"DROP DATABASE IF EXISTS {test_db_name};")
    except Exception:
        pass


@pytest.fixture(autouse=True)
def ensure_pool_clean(pg_concurrency_db):
    """Ensure pool is clean before and after each test."""
    yield
    reset_pg_pool()


# ---------------------------------------------------------------------------
# Phase 3: Concurrency and Uniqueness Constraints
# ---------------------------------------------------------------------------

def test_concurrent_duplicate_model_name_same_workspace(pg_concurrency_db):
    """Test 1: Concurrent creation of models with identical names in the same workspace.
    PostgreSQL uniqueness constraint uq_model_slots_workspace_name must ensure exactly 1
    succeeds and 1 fails with 409 DUPLICATE_MODEL_NAME.
    """
    client = TestClient(app)
    # Obtain a session token
    init_resp = client.get("/api/workspace/models")
    assert init_resp.status_code == 200
    token = client.cookies.get("nr_session")
    assert token is not None

    payload1 = {
        "model_name": "concurrent-dup-gpt",
        "provider": "simulated",
        "model_id": "gpt-dup-1",
        "capability": 3,
        "context_limit": 16000,
        "cost_per_1k": 0.05,
        "latency_ms": 500,
        "failure_rate": 0.0,
        "execution_mode": "simulated",
    }
    payload2 = {
        "model_name": "concurrent-dup-gpt",  # Exact duplicate name
        "provider": "simulated",
        "model_id": "gpt-dup-2",
        "capability": 3,
        "context_limit": 32000,
        "cost_per_1k": 0.08,
        "latency_ms": 600,
        "failure_rate": 0.0,
        "execution_mode": "simulated",
    }

    # Dispatch to slot 1 and slot 2 concurrently
    def send_req(slot: int, payload: dict):
        c = TestClient(app, cookies={"nr_session": token})
        return c.post(f"/api/workspace/models/{slot}", json=payload)

    with ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(send_req, 1, payload1)
        f2 = executor.submit(send_req, 2, payload2)
        r1 = f1.result()
        r2 = f2.result()

    status_codes = sorted([r1.status_code, r2.status_code])
    assert status_codes == [201, 409], f"Expected [201, 409], got {status_codes}"

    failed_resp = r1 if r1.status_code == 409 else r2
    assert failed_resp.json()["error"]["code"] == "DUPLICATE_MODEL_NAME"


def test_concurrent_same_slot_collision(pg_concurrency_db):
    """Test 2: Concurrent creation of two different models targeted at the exact same slot.
    PostgreSQL uniqueness constraint uq_model_slots_workspace_slot must ensure exactly 1
    succeeds and 1 fails with 409 SLOT_OCCUPIED.
    """
    client = TestClient(app)
    init_resp = client.get("/api/workspace/models")
    token = client.cookies.get("nr_session")

    payload1 = {
        "model_name": "slot-racer-alpha",
        "provider": "simulated",
        "model_id": "alpha-1",
        "capability": 2,
        "context_limit": 8000,
        "cost_per_1k": 0.02,
        "latency_ms": 300,
        "execution_mode": "simulated",
    }
    payload2 = {
        "model_name": "slot-racer-beta",
        "provider": "simulated",
        "model_id": "beta-1",
        "capability": 4,
        "context_limit": 16000,
        "cost_per_1k": 0.04,
        "latency_ms": 400,
        "execution_mode": "simulated",
    }

    def send_req(payload: dict):
        c = TestClient(app, cookies={"nr_session": token})
        return c.post("/api/workspace/models/3", json=payload)

    with ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(send_req, payload1)
        f2 = executor.submit(send_req, payload2)
        r1 = f1.result()
        r2 = f2.result()

    status_codes = sorted([r1.status_code, r2.status_code])
    assert status_codes == [201, 409], f"Expected [201, 409], got {status_codes}"

    failed_resp = r1 if r1.status_code == 409 else r2
    assert failed_resp.json()["error"]["code"] == "SLOT_OCCUPIED"


def test_concurrent_distinct_models_creation(pg_concurrency_db):
    """Test 3: Concurrent creation of 6 distinct models into slots 1-6.
    All 6 concurrent requests must succeed with HTTP 201 Created.
    """
    client = TestClient(app)
    init_resp = client.get("/api/workspace/models")
    token = client.cookies.get("nr_session")

    def create_slot(slot: int):
        c = TestClient(app, cookies={"nr_session": token})
        payload = {
            "model_name": f"parallel-model-{slot}",
            "provider": "simulated",
            "model_id": f"p-model-{slot}",
            "capability": (slot % 5) + 1,
            "context_limit": 16000 * slot,
            "cost_per_1k": 0.01 * slot,
            "latency_ms": 200 * slot,
            "execution_mode": "simulated",
        }
        return slot, c.post(f"/api/workspace/models/{slot}", json=payload)

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [executor.submit(create_slot, slot) for slot in range(1, 7)]
        results = [f.result() for f in as_completed(futures)]

    for slot, resp in results:
        assert resp.status_code == 201, f"Slot {slot} failed: {resp.text}"
        assert resp.json()["model_name"] == f"parallel-model-{slot}"

    # Verify workspace now has all 6 slots populated
    check_client = TestClient(app, cookies={"nr_session": token})
    check_resp = check_client.get("/api/workspace/models")
    slots = check_resp.json()["slots"]
    assert len(slots) == 6
    assert all(not s["empty"] for s in slots)


def test_consistent_slot_limits_and_validation_under_concurrency(pg_concurrency_db):
    """Test 4: Consistent slot limits under concurrent requests.
    Valid slots (1-6) succeed or reject cleanly; out-of-bound slots (0, 7) consistently return 400.
    """
    client = TestClient(app)
    client.get("/api/workspace/models")
    token = client.cookies.get("nr_session")

    def call_slot(slot: int):
        c = TestClient(app, cookies={"nr_session": token})
        payload = {
            "model_name": f"bound-test-{slot}",
            "provider": "simulated",
            "model_id": f"b-{slot}",
            "capability": 3,
            "context_limit": 8000,
            "cost_per_1k": 0.05,
            "latency_ms": 500,
            "execution_mode": "simulated",
        }
        return slot, c.post(f"/api/workspace/models/{slot}", json=payload)

    with ThreadPoolExecutor(max_workers=4) as executor:
        f_invalid_high = executor.submit(call_slot, 7)
        f_invalid_low = executor.submit(call_slot, 0)
        f_invalid_neg = executor.submit(call_slot, -1)

    assert f_invalid_high.result()[1].status_code == 400
    assert f_invalid_low.result()[1].status_code == 400
    assert f_invalid_neg.result()[1].status_code == 400


def test_transaction_rollback_after_failed_write(pg_concurrency_db):
    """Test 5: Transaction rollback after a failed write.
    When a database insert violates a constraint, the transaction is atomically rolled back,
    leaving no partial state and permitting immediate subsequent writes.
    """
    raw_token = secrets.token_hex(16)
    token_hash = hash_token(raw_token)
    workspace_id = create_workspace(token_hash)

    # 1. Successful insert into slot 1
    add_model(
        workspace_id,
        1,
        {
            "model_name": "primary-model",
            "provider": "simulated",
            "model_id": "m1",
            "capability": 3,
            "context_limit": 8000,
            "cost_per_1k": 0.1,
            "latency_ms": 500,
        },
    )

    # 2. Failed insert into slot 2 with conflicting model_name
    with pytest.raises(DuplicateModelNameError):
        add_model(
            workspace_id,
            2,
            {
                "model_name": "primary-model",  # Conflict
                "provider": "simulated",
                "model_id": "m2",
                "capability": 3,
                "context_limit": 8000,
                "cost_per_1k": 0.1,
                "latency_ms": 500,
            },
        )

    # 3. Assert slot 2 remains empty (rolled back)
    slot2 = get_slot(workspace_id, 2)
    assert slot2 is None

    # 4. Immediate subsequent write with valid name succeeds
    m2 = add_model(
        workspace_id,
        2,
        {
            "model_name": "secondary-model",
            "provider": "simulated",
            "model_id": "m2",
            "capability": 3,
            "context_limit": 8000,
            "cost_per_1k": 0.1,
            "latency_ms": 500,
        },
    )
    assert m2["model_name"] == "secondary-model"
    assert get_slot(workspace_id, 2) is not None


# ---------------------------------------------------------------------------
# Phase 4: Connection Pool Reliability and Recovery
# ---------------------------------------------------------------------------

def test_failed_transactions_return_connections_in_idle_state(pg_concurrency_db):
    """Test 6: Verify that failed transactions do not leave connections in an aborted or invalid state.
    After an error inside with conn:, the connection returns to IDLE state.
    """
    pool = get_pg_pool()

    # Trigger a database-level error inside a connection checkout
    with pytest.raises(psycopg.Error):
        with pool.connection() as conn:
            with conn:
                conn.execute("SELECT * FROM non_existent_table_for_error_test;")

    # Check out the connection again and verify its status is healthy and IDLE
    with pool.connection() as conn:
        assert conn.closed is False
        assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
        cursor = conn.execute("SELECT 1 AS healthy;")
        row = cursor.fetchone()
        assert row["healthy"] == 1


def test_pool_discards_closed_connection_and_recovers(pg_concurrency_db):
    """Test 7: Verify that if a connection is closed or broken, the pool discards it and
    subsequent requests acquire a fresh connection seamlessly.
    """
    pool = get_pg_pool()

    # Intentionally close a connection
    with pool.connection() as conn:
        conn.close()
        assert conn.closed is True

    # Next checkout must recover and supply an active connection
    with pool.connection() as conn2:
        assert conn2.closed is False
        cursor = conn2.execute("SELECT 42 AS val;")
        assert cursor.fetchone()["val"] == 42


def test_connection_error_propagation_and_sanitization(pg_concurrency_db, monkeypatch):
    """Test 8: How connection errors propagate to the application and ensure credentials are never leaked."""
    bad_url = "postgresql://testuser:SuperSecretPassword123@127.0.0.1:54329/nonexistent_db"
    monkeypatch.setenv("DATABASE_URL", bad_url)
    monkeypatch.setenv("DB_POOL_TIMEOUT", "1.0")
    reset_pg_pool()

    with pytest.raises(DatabaseConnectionError) as exc_info:
        get_pg_pool()

    err_msg = str(exc_info.value)
    # Must NOT expose plaintext password
    assert "SuperSecretPassword123" not in err_msg
    # Must mask password properly
    assert "testuser:***@127.0.0.1" in err_msg


def test_subsequent_healthy_requests_succeed_after_transient_failure(pg_concurrency_db):
    """Test 9: Whether subsequent healthy requests can succeed after transient failure."""
    raw_token = secrets.token_hex(16)
    workspace_id = create_workspace(hash_token(raw_token))

    # Perform a sequence of operations mixing failures and successes
    for i in range(1, 4):
        # 1. Trigger failure: invalid slot
        with pytest.raises(DB_INTEGRITY_ERRORS):
            with get_connection() as conn:
                with conn:
                    # Violate check constraint slot_number between 1 and 6
                    conn.execute(
                        """
                        INSERT INTO model_slots(
                            workspace_id, slot_number, model_name, provider, model_id,
                            capability, context_limit, cost_per_1k, latency_ms,
                            created_at, updated_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW());
                        """,
                        (workspace_id, 99, f"bad-{i}", "simulated", "b", 3, 1000, 0.1, 500),
                    )

        # 2. Subsequent healthy operation immediately succeeds
        slot_data = add_model(
            workspace_id,
            i,
            {
                "model_name": f"healthy-slot-{i}",
                "provider": "simulated",
                "model_id": f"h-{i}",
                "capability": 3,
                "context_limit": 10000,
                "cost_per_1k": 0.05,
                "latency_ms": 300,
            },
        )
        assert slot_data["model_name"] == f"healthy-slot-{i}"

    # Verify all 3 healthy slots exist
    slots = list_slots(workspace_id)
    occupied = [s for s in slots if not s["empty"]]
    assert len(occupied) == 3


def test_pool_exhaustion_timeout(pg_concurrency_db, monkeypatch):
    """Test 10: Connection pool handles exhaustion by timing out cleanly."""
    monkeypatch.setenv("DB_POOL_MIN_SIZE", "1")
    monkeypatch.setenv("DB_POOL_MAX_SIZE", "2")
    monkeypatch.setenv("DB_POOL_TIMEOUT", "0.5")
    reset_pg_pool()

    pool = get_pg_pool()

    # Check out both available connections
    c1 = pool.getconn()
    c2 = pool.getconn()

    try:
        # 3rd checkout must timeout cleanly
        with pytest.raises(psycopg_pool.PoolTimeout):
            pool.getconn(timeout=0.5)
    finally:
        pool.putconn(c1)
        pool.putconn(c2)
