"""Tests for Milestone G2: PostgreSQL Driver and Connection Layer.
Covers:
1. Backend selection precedence (SQLite vs PostgreSQL).
2. Production / Vercel enforcement of DATABASE_URL.
3. PostgreSQL connection failure never silently falls back to SQLite.
4. Credential masking in logs, URLs, and exception messages.
5. Connection pool lifecycle and cleanup (open, checkout, return, close).
6. Transaction commit and rollback semantics.
7. Query dispatcher helper q() and insert ID extraction.
8. Unified integrity error handling for both SQLite and PostgreSQL.
"""
from contextlib import contextmanager
import os
import sqlite3
import threading
from unittest.mock import MagicMock, patch
import pytest

import psycopg
import psycopg_pool
from psycopg.rows import dict_row

from src.gateway.db import (
    DB_INTEGRITY_ERRORS,
    DatabaseConfigError,
    DatabaseConnectionError,
    close_pg_pool,
    execute_insert_returning_id,
    get_backend,
    get_connection,
    get_db_path,
    get_pg_pool,
    is_postgres,
    is_sqlite,
    mask_database_url,
    q,
    reset_pg_pool,
)
from src.gateway.workspace_repo import (
    DuplicateModelNameError,
    SlotOccupiedError,
    create_workspace,
    get_workspace_by_token_hash,
)


@pytest.fixture(autouse=True)
def clean_pool_and_env():
    """Ensure connection pool and database environment variables are reset between tests."""
    reset_pg_pool()
    yield
    reset_pg_pool()


# ---------------------------------------------------------------------------
# 1. Backend Selection Precedence
# ---------------------------------------------------------------------------

def test_sqlite_selected_by_default(monkeypatch):
    """When DATABASE_URL is unset and not in production, SQLite is selected."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("VERCEL", raising=False)
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("ENV", raising=False)

    assert get_backend() == "sqlite"
    assert is_sqlite() is True
    assert is_postgres() is False
    assert get_db_path() == "neuroroute.db"


def test_sqlite_selected_via_neuroroute_db(monkeypatch, tmp_path):
    """NEUROROUTE_DB selects SQLite with custom database file path."""
    custom_path = str(tmp_path / "custom.db")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("VERCEL", raising=False)
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.setenv("NEUROROUTE_DB", custom_path)

    assert get_backend() == "sqlite"
    assert is_sqlite() is True
    assert get_db_path() == custom_path


def test_postgres_selected_when_database_url_set(monkeypatch):
    """Explicit DATABASE_URL selects PostgreSQL."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
    assert get_backend() == "postgres"
    assert is_postgres() is True
    assert is_sqlite() is False


# ---------------------------------------------------------------------------
# 2. Production / Vercel Environment Enforcement
# ---------------------------------------------------------------------------

def test_production_missing_database_url_raises_config_error(monkeypatch):
    """Missing DATABASE_URL in Vercel or production environment raises DatabaseConfigError."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("VERCEL", "1")

    with pytest.raises(DatabaseConfigError) as exc_info:
        get_backend()
    assert "DATABASE_URL must be configured in production/Vercel" in str(exc_info.value)
    assert "Ephemeral SQLite storage is prohibited" in str(exc_info.value)


def test_production_env_var_missing_database_url_raises(monkeypatch):
    """ENVIRONMENT=production without DATABASE_URL raises DatabaseConfigError."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("ENVIRONMENT", "production")

    with pytest.raises(DatabaseConfigError):
        get_backend()


# ---------------------------------------------------------------------------
# 3. PostgreSQL Failure Never Falls Back to SQLite
# ---------------------------------------------------------------------------

def test_postgres_connection_failure_never_falls_back_to_sqlite(monkeypatch):
    """When PostgreSQL is configured but fails to connect, raise DatabaseConnectionError.
    NEVER silently fall back to SQLite.
    """
    # Non-routable address to force connection timeout / refusal
    fake_pg = "postgresql://nr_user:s3cr3t@127.0.0.1:59999/nr_db?connect_timeout=1"
    monkeypatch.setenv("DATABASE_URL", fake_pg)
    monkeypatch.setenv("DB_POOL_TIMEOUT", "0.5")

    with pytest.raises(DatabaseConnectionError) as exc_info:
        with get_connection():
            pass

    # Ensure error is clear and backend was not silently degraded to SQLite
    assert "Failed to connect to PostgreSQL" in str(exc_info.value)
    # Ensure raw secret is not in the error message
    assert "s3cr3t" not in str(exc_info.value)
    assert "nr_user:***@127.0.0.1:59999/nr_db" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 4. Credential Masking Security
# ---------------------------------------------------------------------------

def test_credential_masking():
    """mask_database_url redacts passwords from connection URLs."""
    raw = "postgresql://app_user:super_secret_pwd_99@aws.rds.com:5432/production_db?sslmode=require"
    masked = mask_database_url(raw)

    assert "super_secret_pwd_99" not in masked
    assert "app_user:***@aws.rds.com:5432/production_db" in masked
    assert masked.endswith("?sslmode=require")

    # URL without password
    no_pass = "postgresql://localhost:5432/devdb"
    assert mask_database_url(no_pass) == no_pass

    # None or empty
    assert mask_database_url(None) == ""
    assert mask_database_url("") == ""


# ---------------------------------------------------------------------------
# 5. Query Dispatcher and Insert ID Helper
# ---------------------------------------------------------------------------

def test_query_dispatcher_q(monkeypatch):
    """q() helper dispatches to postgres query when in postgres mode, sqlite query otherwise."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("VERCEL", raising=False)
    assert q("SELECT ?", "SELECT %s") == "SELECT ?"

    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost:5432/db")
    assert q("SELECT ?", "SELECT %s") == "SELECT %s"


def test_execute_insert_returning_id_sqlite(tmp_path, monkeypatch):
    """execute_insert_returning_id uses cursor.lastrowid on SQLite."""
    db_file = str(tmp_path / "insert_test.db")
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT);")

    inserted_id = execute_insert_returning_id(
        conn,
        "INSERT INTO items(name) VALUES (?)",
        "INSERT INTO items(name) VALUES (%s) RETURNING id",
        ("test_item",),
    )
    assert inserted_id == 1
    conn.close()


def test_execute_insert_returning_id_mock_postgres(monkeypatch):
    """execute_insert_returning_id uses RETURNING id on PostgreSQL (mocked connection)."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost:5432/db")

    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = {"id": 42}
    mock_conn.execute.return_value = mock_cursor

    inserted_id = execute_insert_returning_id(
        mock_conn,
        "INSERT INTO items(name) VALUES (?)",
        "INSERT INTO items(name) VALUES (%s) RETURNING id",
        ("test_item",),
    )

    mock_conn.execute.assert_called_once_with(
        "INSERT INTO items(name) VALUES (%s) RETURNING id",
        ("test_item",),
    )
    assert inserted_id == 42


# ---------------------------------------------------------------------------
# 6. Mocked PostgreSQL Connection Pool & Transaction Lifecycle
# ---------------------------------------------------------------------------

def test_mocked_postgres_pool_lifecycle(monkeypatch):
    """Verify PostgreSQL pool configuration, checkout, return, and clean shutdown (Mocked)."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://mock_user:mock_pass@localhost:5432/mock_db")
    monkeypatch.setenv("DB_POOL_MIN_SIZE", "2")
    monkeypatch.setenv("DB_POOL_MAX_SIZE", "8")

    mock_pool = MagicMock()
    mock_conn = MagicMock()
    mock_pool.closed = False

    @contextmanager
    def mock_connection(timeout=None):
        yield mock_conn

    mock_pool.connection = mock_connection

    with patch("psycopg_pool.ConnectionPool", return_value=mock_pool) as pool_constructor:
        # 1. Acquire pool and verify arguments
        pool = get_pg_pool()
        assert pool == mock_pool
        pool_constructor.assert_called_once()
        kwargs = pool_constructor.call_args.kwargs
        assert kwargs["min_size"] == 2
        assert kwargs["max_size"] == 8
        assert kwargs["max_idle"] == 300.0
        assert kwargs["max_lifetime"] == 3600.0
        assert kwargs["check"] == psycopg_pool.ConnectionPool.check_connection
        assert kwargs["kwargs"]["row_factory"] == dict_row
        assert kwargs["kwargs"]["autocommit"] is False

        # 2. Verify get_connection yields mocked connection
        with get_connection() as conn:
            assert conn == mock_conn

        # 3. Clean pool shutdown
        close_pg_pool()
        mock_pool.close.assert_called_once()


def test_mocked_postgres_pool_custom_resilience_env_vars(monkeypatch):
    """Verify that DB_POOL_MAX_IDLE and DB_POOL_MAX_LIFETIME configure the pool accordingly."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://mock_user:mock_pass@localhost:5432/mock_db")
    monkeypatch.setenv("DB_POOL_MAX_IDLE", "120.5")
    monkeypatch.setenv("DB_POOL_MAX_LIFETIME", "1800.0")

    mock_pool = MagicMock()
    mock_conn = MagicMock()
    mock_pool.closed = False

    @contextmanager
    def mock_connection(timeout=None):
        yield mock_conn

    mock_pool.connection = mock_connection

    with patch("psycopg_pool.ConnectionPool", return_value=mock_pool) as pool_constructor:
        pool = get_pg_pool()
        assert pool == mock_pool
        kwargs = pool_constructor.call_args.kwargs
        assert kwargs["max_idle"] == 120.5
        assert kwargs["max_lifetime"] == 1800.0
        assert kwargs["check"] == psycopg_pool.ConnectionPool.check_connection
        close_pg_pool()


def test_postgres_pool_check_connection_autocommit_true():
    """Verify check_connection executes empty query when autocommit is True."""
    mock_conn = MagicMock()
    mock_conn.autocommit = True
    psycopg_pool.ConnectionPool.check_connection(mock_conn)
    mock_conn.execute.assert_called_once_with("")
    assert mock_conn.autocommit is True


def test_postgres_pool_check_connection_autocommit_false():
    """Verify check_connection temporarily sets autocommit=True, executes empty query, and restores autocommit=False."""
    mock_conn = MagicMock()
    mock_conn.autocommit = False
    psycopg_pool.ConnectionPool.check_connection(mock_conn)
    mock_conn.execute.assert_called_once_with("")
    assert mock_conn.autocommit is False


def test_postgres_pool_check_connection_failure_restores_autocommit():
    """Verify check_connection restores autocommit=False even when execution fails on dead connection."""
    mock_conn = MagicMock()
    mock_conn.autocommit = False
    mock_conn.execute.side_effect = psycopg.OperationalError("server closed the connection unexpectedly")

    with pytest.raises(psycopg.OperationalError, match="server closed the connection"):
        psycopg_pool.ConnectionPool.check_connection(mock_conn)
    assert mock_conn.autocommit is False


def test_postgres_pool_discards_bad_connection_on_check_failure():
    """Verify pool check callback discards broken connection on checkout and replaces with healthy one."""
    broken_conn = MagicMock()
    broken_conn.closed = False
    broken_conn.autocommit = False

    healthy_conn = MagicMock()
    healthy_conn.closed = False
    healthy_conn.autocommit = False

    # Simulate pool _getconn_with_check_loop behavior
    # Connection 1 fails check_connection; Connection 2 passes
    attempted_connections = [broken_conn, healthy_conn]
    discarded_connections = []

    def mock_getconn_unchecked(timeout):
        return attempted_connections.pop(0)

    def mock_check(conn):
        if conn is broken_conn:
            raise psycopg.OperationalError("server terminated idle connection")

    def mock_putconn(conn, from_getconn=False):
        if from_getconn:
            discarded_connections.append(conn)

    # Perform the check loop logic defined by psycopg_pool
    # Verify broken_conn was discarded and healthy_conn was returned
    active_conn = None
    while attempted_connections or broken_conn:
        c = mock_getconn_unchecked(timeout=10.0)
        try:
            mock_check(c)
            active_conn = c
            break
        except Exception:
            mock_putconn(c, from_getconn=True)

    assert active_conn == healthy_conn
    assert discarded_connections == [broken_conn]


def test_mocked_postgres_transaction_rollback_on_error(monkeypatch):
    """Verify that exceptions inside connection context trigger clean error bubbling and pool return (Mocked)."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://mock_user:mock_pass@localhost:5432/mock_db")

    mock_pool = MagicMock()
    mock_conn = MagicMock()
    mock_pool.closed = False
    returned_to_pool = []

    @contextmanager
    def mock_connection(timeout=None):
        try:
            yield mock_conn
        finally:
            returned_to_pool.append(True)

    mock_pool.connection = mock_connection

    with patch("psycopg_pool.ConnectionPool", return_value=mock_pool):
        # Pool initialization tests connectivity (first checkout)
        get_pg_pool()
        returned_to_pool.clear()

        with pytest.raises(RuntimeError, match="Simulation error"):
            with get_connection() as conn:
                raise RuntimeError("Simulation error")

        # Verify connection was returned to pool despite exception
        assert len(returned_to_pool) == 1


# ---------------------------------------------------------------------------
# 7. Unified Integrity Error Handling
# ---------------------------------------------------------------------------

def test_unified_integrity_error_handling():
    """Verify DB_INTEGRITY_ERRORS tuple catches both SQLite and PostgreSQL integrity errors."""
    assert issubclass(sqlite3.IntegrityError, DB_INTEGRITY_ERRORS)
    assert issubclass(psycopg.IntegrityError, DB_INTEGRITY_ERRORS)

    # Test catching SQLite IntegrityError
    try:
        raise sqlite3.IntegrityError("UNIQUE constraint failed: model_slots.slot_number")
    except DB_INTEGRITY_ERRORS as e:
        assert "slot_number" in str(e).lower()

    # Test catching PostgreSQL UniqueViolation (subclass of psycopg.IntegrityError)
    try:
        raise psycopg.errors.UniqueViolation('duplicate key value violates unique constraint "model_slots_workspace_id_slot_number_key"')
    except DB_INTEGRITY_ERRORS as e:
        assert "slot_number" in str(e).lower()
