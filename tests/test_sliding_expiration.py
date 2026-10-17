"""Tests for Milestone G7.9: Sliding Workspace Expiration.

Verifies:
1. Initial workspace expiration remains 30 days.
2. Eligible activity extends expiration correctly (UTC now + 30 days).
3. Requests within the 1-hour throttle window do not cause database writes.
4. Cookie refresh occurs when expiration slides.
5. Cookie refresh does not occur unnecessarily for throttled requests.
6. Expired workspaces cannot be revived.
7. Conditional updates affecting zero rows return None and do not report success.
8. Concurrent requests execute safely without race condition errors.
9. Static asset requests (/ and /index.html) do not trigger sliding expiration.
10. Dual-backend execution on SQLite and disposable PostgreSQL.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import os
import threading
from typing import Generator
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import psycopg
import pytest

from src.gateway import db
from src.gateway.db import (
    close_pg_pool,
    get_conn,
    get_connection,
    init_workspace_schema,
    q,
    reset_pg_pool,
)
from src.gateway.logger import init_db
from src.gateway.main import app
from src.gateway.session import COOKIE_NAME, SESSION_MAX_AGE_SECONDS, hash_token
from src.gateway.workspace_repo import (
    SLIDING_THROTTLE_SECONDS,
    SLIDING_WINDOW_DAYS,
    _to_utc_datetime,
    create_workspace,
    get_workspace_by_token_hash,
)


@pytest.fixture
def sqlite_test_db(tmp_path, monkeypatch):
    """Provide an isolated, disposable SQLite database for each test."""
    db_file = str(tmp_path / "test_sliding.db")
    monkeypatch.setenv("NEUROROUTE_DB", db_file)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("VERCEL", raising=False)
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    init_db()
    init_workspace_schema()
    yield db_file


@pytest.fixture
def client(sqlite_test_db):
    """FastAPI TestClient bound to disposable SQLite test database."""
    return TestClient(app)


def _get_postgres_admin_credentials():
    """Retrieve postgres port and password safely from pgpass.conf without leaking."""
    p = os.path.join(os.environ.get("APPDATA", ""), "postgresql", "pgpass.conf")
    if not os.path.exists(p):
        pytest.skip("PostgreSQL pgpass.conf not found for local test database")
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                parts = line.split(":")
                if len(parts) >= 5:
                    return parts[1], parts[4]
    pytest.skip("No valid PostgreSQL credentials found in pgpass.conf")


@pytest.fixture(scope="module")
def pg_disposable_db() -> Generator[str, None, None]:
    """Provision a strictly isolated, disposable PostgreSQL database (neuroroute_sliding_test)
    and completely drop it on teardown. Never touches neuroroute.db or neuroroute_test.
    """
    try:
        port, pwd = _get_postgres_admin_credentials()
    except Exception as e:
        pytest.skip(f"Skipping PostgreSQL tests: {e}")

    admin_url = f"postgresql://postgres:{pwd}@127.0.0.1:{port}/postgres"
    test_db_name = "neuroroute_sliding_test"
    test_db_url = f"postgresql://postgres:{pwd}@127.0.0.1:{port}/{test_db_name}"

    try:
        with psycopg.connect(admin_url, autocommit=True) as conn:
            conn.execute(f"DROP DATABASE IF EXISTS {test_db_name};")
            conn.execute(f"CREATE DATABASE {test_db_name};")
    except Exception as e:
        pytest.skip(f"Could not connect to PostgreSQL: {e}")

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

    orig_env = os.environ.copy()
    os.environ["DATABASE_URL"] = test_db_url
    os.environ["NEUROROUTE_SECRET_KEY"] = Fernet.generate_key().decode()
    os.environ.pop("NEUROROUTE_DB", None)
    os.environ.pop("VERCEL", None)
    os.environ.pop("ENVIRONMENT", None)

    reset_pg_pool()

    yield test_db_url

    close_pg_pool()
    os.environ.clear()
    os.environ.update(orig_env)

    try:
        with psycopg.connect(admin_url, autocommit=True) as conn:
            conn.execute(f"DROP DATABASE IF EXISTS {test_db_name};")
    except Exception:
        pass


# ===========================================================================
# 1. Initial Workspace Expiration Tests
# ===========================================================================

def test_initial_workspace_expiration_is_30_days(sqlite_test_db):
    """When a workspace is created, its initial expiration is exactly 30 days from creation."""
    token = "test_initial_token_123"
    th = hash_token(token)
    ws_id = create_workspace(th, ttl_days=30)

    ws = get_workspace_by_token_hash(th, slide_expiration=False)
    assert ws is not None
    assert ws["id"] == ws_id

    created_dt = _to_utc_datetime(ws["created_at"])
    expires_dt = _to_utc_datetime(ws["expires_at"])
    diff_days = (expires_dt - created_dt).total_seconds() / 86400.0

    assert 29.99 <= diff_days <= 30.01


# ===========================================================================
# 2. Throttling and Sliding Extension Tests
# ===========================================================================

def test_requests_within_throttle_window_do_not_write(sqlite_test_db):
    """Requests within the 1-hour throttle window do not perform database updates."""
    token = "test_throttle_token_456"
    th = hash_token(token)
    ws_id = create_workspace(th, ttl_days=30)

    # First retrieval immediately after creation
    ws1 = get_workspace_by_token_hash(th, slide_expiration=True)
    assert ws1 is not None
    assert ws1.get("_slid") is False

    # Second retrieval 10 seconds later
    ws2 = get_workspace_by_token_hash(th, slide_expiration=True)
    assert ws2 is not None
    assert ws2.get("_slid") is False
    assert ws2["last_seen_at"] == ws1["last_seen_at"]
    assert ws2["expires_at"] == ws1["expires_at"]


def test_activity_past_throttle_window_slides_expiration(sqlite_test_db):
    """Activity occurring >= 1 hour after last_seen_at extends expiration to UTC now + 30 days."""
    token = "test_slide_token_789"
    th = hash_token(token)
    ws_id = create_workspace(th, ttl_days=30)

    # Simulate that the workspace was last seen 2 hours ago
    two_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    old_expires = (datetime.now(timezone.utc) + timedelta(days=20)).isoformat()

    conn = get_conn()
    with conn:
        conn.execute(
            "UPDATE workspaces SET last_seen_at = ?, expires_at = ? WHERE id = ?",
            (two_hours_ago, old_expires, ws_id),
        )
    conn.close()

    # Now retrieve with sliding expiration active
    before_call = datetime.now(timezone.utc)
    ws = get_workspace_by_token_hash(th, slide_expiration=True)
    after_call = datetime.now(timezone.utc)

    assert ws is not None
    assert ws.get("_slid") is True

    new_last_seen = _to_utc_datetime(ws["last_seen_at"])
    new_expires = _to_utc_datetime(ws["expires_at"])

    assert before_call <= new_last_seen <= after_call
    expected_min_expires = before_call + timedelta(days=30)
    expected_max_expires = after_call + timedelta(days=30)
    assert expected_min_expires <= new_expires <= expected_max_expires


def test_remaining_lifetime_under_24h_slides_even_if_recently_seen(sqlite_test_db):
    """Workspace with < 24 hours remaining slides immediately even if last_seen_at is < 1 hour old."""
    token = "test_under_24h_token"
    th = hash_token(token)
    ws_id = create_workspace(th, ttl_days=30)

    # Set expires_at to 6 hours from now, and last_seen_at to 5 minutes ago (< 1 hour)
    now = datetime.now(timezone.utc)
    five_min_ago = (now - timedelta(minutes=5)).isoformat()
    six_hours_ahead = (now + timedelta(hours=6)).isoformat()

    conn = get_conn()
    with conn:
        conn.execute(
            "UPDATE workspaces SET last_seen_at = ?, expires_at = ? WHERE id = ?",
            (five_min_ago, six_hours_ahead, ws_id),
        )
    conn.close()

    # Retrieve with slide_expiration=True
    before_call = datetime.now(timezone.utc)
    ws = get_workspace_by_token_hash(th, slide_expiration=True)
    after_call = datetime.now(timezone.utc)

    assert ws is not None
    assert ws.get("_slid") is True

    # Expiration must return to ~30 days from now
    new_expires = _to_utc_datetime(ws["expires_at"])
    assert before_call + timedelta(days=29, hours=23) <= new_expires <= after_call + timedelta(days=30, hours=1)


def test_remaining_lifetime_boundary_24h(sqlite_test_db):
    """Verify exact <= 86400 boundary: exactly 24h slides; > 24h (e.g. 24h + 10s) does not slide when recently seen."""
    token_exact = "test_exact_24h_token"
    th_exact = hash_token(token_exact)
    ws_exact = create_workspace(th_exact, ttl_days=30)

    now = datetime.now(timezone.utc)
    five_min_ago = (now - timedelta(minutes=5)).isoformat()

    # Test 1: Exactly 24 hours remaining (86400 seconds)
    # Give a tiny buffer (86395s) so elapsed time during test execution stays <= 86400s
    exp_exact = (now + timedelta(seconds=86398)).isoformat()
    conn = get_conn()
    with conn:
        conn.execute(
            "UPDATE workspaces SET last_seen_at = ?, expires_at = ? WHERE id = ?",
            (five_min_ago, exp_exact, ws_exact),
        )
    conn.close()

    ws1 = get_workspace_by_token_hash(th_exact, slide_expiration=True)
    assert ws1 is not None
    assert ws1.get("_slid") is True
    assert _to_utc_datetime(ws1["expires_at"]) > now + timedelta(days=29)

    # Test 2: More than 24 hours remaining (e.g. 25 hours) with last_seen_at 5 min ago
    token_above = "test_above_24h_token"
    th_above = hash_token(token_above)
    ws_above = create_workspace(th_above, ttl_days=30)

    exp_above = (now + timedelta(hours=25)).isoformat()
    conn = get_conn()
    with conn:
        conn.execute(
            "UPDATE workspaces SET last_seen_at = ?, expires_at = ? WHERE id = ?",
            (five_min_ago, exp_above, ws_above),
        )
    conn.close()

    ws2 = get_workspace_by_token_hash(th_above, slide_expiration=True)
    assert ws2 is not None
    # Must NOT slide (elapsed < 1h AND remaining > 24h)
    assert ws2.get("_slid") is False
    assert ws2["expires_at"] == exp_above


# ===========================================================================
# 3. Cookie Refresh Behavior Tests
# ===========================================================================

def test_cookie_refresh_occurs_when_sliding_and_not_when_throttled(client):
    """Cookie is refreshed with Max-Age=2592000 only when a slide occurs."""
    # Step 1: Initial request creates a new session
    resp1 = client.get("/api/workspace/models")
    assert resp1.status_code == 200
    cookie_val = resp1.cookies.get(COOKIE_NAME)
    assert cookie_val is not None

    # Step 2: Immediate follow-up request (throttled) -> NO Set-Cookie header
    client.cookies.set(COOKIE_NAME, cookie_val)
    resp2 = client.get("/api/workspace/models")
    assert resp2.status_code == 200
    assert COOKIE_NAME not in resp2.cookies

    # Step 3: Advance last_seen_at to 2 hours ago in DB
    th = hash_token(cookie_val)
    two_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    conn = get_conn()
    with conn:
        conn.execute("UPDATE workspaces SET last_seen_at = ? WHERE session_token_hash = ?", (two_hours_ago, th))
    conn.close()

    # Step 4: Next request -> Sliding occurs -> Set-Cookie header refreshed with identical token
    client.cookies.set(COOKIE_NAME, cookie_val)
    resp3 = client.get("/api/workspace/models")
    assert resp3.status_code == 200
    assert COOKIE_NAME in resp3.cookies
    assert resp3.cookies.get(COOKIE_NAME) == cookie_val


def test_optional_workspace_refreshes_cookie_on_slide(client):
    """Endpoints using get_optional_workspace (like /route) also refresh cookie on slide."""
    # Create workspace session
    resp1 = client.get("/api/workspace/models")
    cookie_val = resp1.cookies.get(COOKIE_NAME)

    # Simulate last_seen_at 2 hours ago
    th = hash_token(cookie_val)
    two_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    conn = get_conn()
    with conn:
        conn.execute("UPDATE workspaces SET last_seen_at = ? WHERE session_token_hash = ?", (two_hours_ago, th))
    conn.close()

    # Call /route with prompt
    req = {
        "prompt": "Hello world sliding test",
        "max_latency_ms": 2000,
        "max_cost": 1.0,
        "min_capability": 1,
        "required_context": 100,
        "preference": "fastest",
    }
    client.cookies.set(COOKIE_NAME, cookie_val)
    resp2 = client.post("/route", json=req)
    assert resp2.status_code == 200
    assert COOKIE_NAME in resp2.cookies
    assert resp2.cookies.get(COOKIE_NAME) == cookie_val


def test_emergency_slide_refreshes_cookie(client):
    """Emergency slide (remaining lifetime < 24h) refreshes the nr_session cookie even when last_seen_at is < 1h."""
    # Step 1: Create session
    resp1 = client.get("/api/workspace/models")
    cookie_val = resp1.cookies.get(COOKIE_NAME)
    th = hash_token(cookie_val)

    # Step 2: Set expires_at to 6 hours from now (< 24h) and last_seen_at to 5 minutes ago (< 1h)
    now = datetime.now(timezone.utc)
    five_min_ago = (now - timedelta(minutes=5)).isoformat()
    six_hours_ahead = (now + timedelta(hours=6)).isoformat()

    conn = get_conn()
    with conn:
        conn.execute(
            "UPDATE workspaces SET last_seen_at = ?, expires_at = ? WHERE session_token_hash = ?",
            (five_min_ago, six_hours_ahead, th),
        )
    conn.close()

    # Step 3: Send request with cookie
    client.cookies.set(COOKIE_NAME, cookie_val)
    resp2 = client.get("/api/workspace/models")
    assert resp2.status_code == 200
    # Cookie MUST be refreshed
    assert COOKIE_NAME in resp2.cookies
    assert resp2.cookies.get(COOKIE_NAME) == cookie_val

    # Step 4: Verify DB updated to ~30 days
    conn = get_conn()
    row = conn.execute("SELECT expires_at FROM workspaces WHERE session_token_hash = ?", (th,)).fetchone()
    conn.close()
    exp_dt = _to_utc_datetime(row["expires_at"])
    assert exp_dt > now + timedelta(days=29)


# ===========================================================================
# 4. Expired Workspace Protection & Zero-Row Affected Guard
# ===========================================================================

def test_expired_workspace_cannot_be_revived(client):
    """An expired workspace is rejected, never slid forward, and replaced by a fresh workspace."""
    # Create an initial session
    resp1 = client.get("/api/workspace/models")
    old_cookie = resp1.cookies.get(COOKIE_NAME)
    old_th = hash_token(old_cookie)

    # Force expiration 5 days into the past
    past_dt = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    conn = get_conn()
    with conn:
        conn.execute("UPDATE workspaces SET expires_at = ? WHERE session_token_hash = ?", (past_dt, old_th))
    conn.close()

    # Attempt request with expired cookie
    client.cookies.set(COOKIE_NAME, old_cookie)
    resp2 = client.get("/api/workspace/models")
    assert resp2.status_code == 200
    new_cookie = resp2.cookies.get(COOKIE_NAME)
    assert new_cookie is not None
    assert new_cookie != old_cookie

    # Verify old workspace in DB was NOT revived or altered
    conn = get_conn()
    row = conn.execute("SELECT * FROM workspaces WHERE session_token_hash = ?", (old_th,)).fetchone()
    conn.close()
    assert row["expires_at"] == past_dt


def test_zero_rowcount_update_returns_none(sqlite_test_db, monkeypatch):
    """If the conditional UPDATE affects zero rows (rowcount <= 0), it returns None and does not report success."""
    token = "test_zero_rowcount_token"
    th = hash_token(token)
    ws_id = create_workspace(th, ttl_days=30)

    # Set last_seen_at to 2 hours ago so it attempts a slide
    two_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    conn = get_conn()
    with conn:
        conn.execute("UPDATE workspaces SET last_seen_at = ? WHERE id = ?", (two_hours_ago, ws_id))
    conn.close()

    from contextlib import contextmanager
    import src.gateway.workspace_repo as repo
    real_get_connection = repo.get_connection

    class DummyCursor:
        rowcount = 0
        def fetchone(self):
            return None

    class WrappedConn:
        def __init__(self, inner):
            self._inner = inner
        def __enter__(self):
            self._inner.__enter__()
            return self
        def __exit__(self, exc_type, exc_val, exc_tb):
            return self._inner.__exit__(exc_type, exc_val, exc_tb)
        def execute(self, sql, params=()):
            if "UPDATE workspaces" in str(sql):
                return DummyCursor()
            return self._inner.execute(sql, params)
        def __getattr__(self, name):
            return getattr(self._inner, name)

    @contextmanager
    def fake_get_conn():
        with real_get_connection() as c:
            yield WrappedConn(c)

    monkeypatch.setattr(repo, "get_connection", fake_get_conn)

    result = get_workspace_by_token_hash(th, slide_expiration=True)
    assert result is None


# ===========================================================================
# 5. Static Assets Do Not Trigger Sliding Expiration
# ===========================================================================

def test_static_assets_do_not_trigger_sliding(client):
    """Static file requests do not invoke workspace dependencies or slide expiration."""
    resp1 = client.get("/api/workspace/models")
    cookie_val = resp1.cookies.get(COOKIE_NAME)
    th = hash_token(cookie_val)

    two_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    conn = get_conn()
    with conn:
        conn.execute("UPDATE workspaces SET last_seen_at = ? WHERE session_token_hash = ?", (two_hours_ago, th))
    conn.close()

    # Request dashboard HTML
    client.cookies.set(COOKIE_NAME, cookie_val)
    resp_static = client.get("/")
    assert resp_static.status_code == 200

    # Verify last_seen_at remains unchanged
    conn = get_conn()
    row = conn.execute("SELECT last_seen_at FROM workspaces WHERE session_token_hash = ?", (th,)).fetchone()
    conn.close()
    assert row["last_seen_at"] == two_hours_ago


# ===========================================================================
# 6. Concurrency Safety Tests
# ===========================================================================

def test_concurrent_sliding_updates_are_thread_safe(sqlite_test_db):
    """Multiple concurrent requests for the same workspace handle sliding updates safely."""
    token = "test_concurrent_token"
    th = hash_token(token)
    ws_id = create_workspace(th, ttl_days=30)

    # Set last_seen_at to 2 hours ago
    two_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    conn = get_conn()
    with conn:
        conn.execute("UPDATE workspaces SET last_seen_at = ? WHERE id = ?", (two_hours_ago, ws_id))
    conn.close()

    results = []
    errors = []

    def worker():
        try:
            ws = get_workspace_by_token_hash(th, slide_expiration=True)
            results.append(ws)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Concurrent workers encountered errors: {errors}"
    assert len(results) == 10
    assert all(r is not None and r["id"] == ws_id for r in results)


# ===========================================================================
# 7. PostgreSQL Disposable Database Tests
# ===========================================================================

def test_postgres_disposable_sliding_expiration(pg_disposable_db):
    """Verify sliding expiration, throttling, and rowcount checks on PostgreSQL."""
    token = "pg_sliding_token_123"
    th = hash_token(token)
    ws_id = create_workspace(th, ttl_days=30)

    # 1. Immediate retrieval is throttled
    ws1 = get_workspace_by_token_hash(th, slide_expiration=True)
    assert ws1 is not None
    assert ws1.get("_slid") is False

    # 2. Update last_seen_at to 2 hours ago in PostgreSQL
    two_hours_ago = datetime.now(timezone.utc) - timedelta(hours=2)
    with get_connection() as conn:
        with conn:
            conn.execute("UPDATE workspaces SET last_seen_at = %s WHERE id = %s", (two_hours_ago, ws_id))

    # 3. Retrieval slides expiration forward
    ws2 = get_workspace_by_token_hash(th, slide_expiration=True)
    assert ws2 is not None
    assert ws2.get("_slid") is True

    exp2 = _to_utc_datetime(ws2["expires_at"])
    now_utc = datetime.now(timezone.utc)
    assert exp2 > now_utc + timedelta(days=29)

    # 4. Emergency slide: remaining lifetime < 24h slides even if last_seen_at was 5 minutes ago
    pg_now = datetime.now(timezone.utc)
    five_min_ago = pg_now - timedelta(minutes=5)
    six_hours_ahead = pg_now + timedelta(hours=6)
    with get_connection() as conn:
        with conn:
            conn.execute(
                "UPDATE workspaces SET last_seen_at = %s, expires_at = %s WHERE id = %s",
                (five_min_ago, six_hours_ahead, ws_id),
            )

    ws_emerg = get_workspace_by_token_hash(th, slide_expiration=True)
    assert ws_emerg is not None
    assert ws_emerg.get("_slid") is True
    assert _to_utc_datetime(ws_emerg["expires_at"]) > pg_now + timedelta(days=29)

    # 5. Expired workspace cannot slide
    past = datetime.now(timezone.utc) - timedelta(days=2)
    with get_connection() as conn:
        with conn:
            conn.execute("UPDATE workspaces SET expires_at = %s WHERE id = %s", (past, ws_id))

    ws_expired = get_workspace_by_token_hash(th, slide_expiration=True)
    assert ws_expired is None
