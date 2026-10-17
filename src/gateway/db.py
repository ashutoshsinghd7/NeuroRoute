"""Database connection and schema management for NeuroRoute.
Supports dual-backend architecture:
- SQLite (local development and isolated test databases)
- PostgreSQL via Psycopg 3 with managed connection pooling

Deterministic Backend Selection Rules:
1. Production / Vercel validation:
   If VERCEL or ENVIRONMENT/ENV is 'production', DATABASE_URL must be configured.
   Missing DATABASE_URL in production raises DatabaseConfigError.
2. Explicit Database Selection:
   - If DATABASE_URL is set and non-empty: selects PostgreSQL.
   - If PostgreSQL connection fails: raises DatabaseConnectionError.
     NEVER silently falls back to SQLite.
   - Else (no DATABASE_URL): selects SQLite.
     Uses NEUROROUTE_DB if set, otherwise falls back to local 'neuroroute.db'.
3. Credential Security:
   - Database passwords and full connection strings are never logged in plaintext.
   - mask_database_url() redacts credentials in all logs and error messages.
"""
import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Generator, Optional
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg.rows import dict_row
import psycopg_pool


class DatabaseConfigError(Exception):
    """Raised when database configuration is invalid or missing in production."""
    pass


class DatabaseConnectionError(Exception):
    """Raised when connecting to the database fails."""
    pass


class DatabaseIntegrityError(Exception):
    """Unified database integrity error."""
    pass


DB_INTEGRITY_ERRORS = (sqlite3.IntegrityError, psycopg.IntegrityError)

_pg_pool: Optional[psycopg_pool.ConnectionPool] = None
_pool_lock = threading.Lock()


def mask_database_url(url: Optional[str]) -> str:
    """Mask the password in a database URL for safe logging and error reporting."""
    if not url:
        return ""
    try:
        parsed = urlsplit(url)
        if parsed.password:
            netloc = f"{parsed.username}:***@{parsed.hostname}"
            if parsed.port:
                netloc += f":{parsed.port}"
            masked = parsed._replace(netloc=netloc)
            return urlunsplit(masked)
        return url
    except Exception:
        return "[REDACTED_DATABASE_URL]"


def is_production_environment() -> bool:
    """Return True if running in production or Vercel serverless environment."""
    return bool(
        os.environ.get("VERCEL")
        or os.environ.get("ENVIRONMENT") == "production"
        or os.environ.get("ENV") == "production"
        or os.environ.get("NODE_ENV") == "production"
    )


def get_database_url() -> Optional[str]:
    """Return configured PostgreSQL DATABASE_URL or None."""
    url = os.environ.get("DATABASE_URL", "").strip()
    return url if url else None


def get_backend() -> str:
    """Determine the active database backend ('postgres' or 'sqlite').
    Enforces deterministic configuration precedence and validation.
    """
    db_url = get_database_url()
    is_prod = is_production_environment()

    if is_prod and not db_url:
        raise DatabaseConfigError(
            "DATABASE_URL must be configured in production/Vercel environment. "
            "Ephemeral SQLite storage is prohibited."
        )

    if db_url:
        return "postgres"
    return "sqlite"


def is_postgres() -> bool:
    """Return True if active backend is PostgreSQL."""
    return get_backend() == "postgres"


def is_sqlite() -> bool:
    """Return True if active backend is SQLite."""
    return get_backend() == "sqlite"


def get_db_path() -> str:
    """Return the active SQLite database path based on environment variables."""
    return os.environ.get(
        "NEUROROUTE_DB",
        "/tmp/neuroroute.db" if os.environ.get("VERCEL") else "neuroroute.db",
    )


def get_sqlite_conn() -> sqlite3.Connection:
    """Create a new SQLite connection with foreign keys and row factory configured."""
    db_path = get_db_path()
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.row_factory = sqlite3.Row
    return conn


def get_pg_pool() -> psycopg_pool.ConnectionPool:
    """Retrieve or lazily initialize the PostgreSQL connection pool."""
    global _pg_pool
    if _pg_pool is not None and not _pg_pool.closed:
        return _pg_pool

    with _pool_lock:
        if _pg_pool is not None and not _pg_pool.closed:
            return _pg_pool

        db_url = get_database_url()
        if not db_url:
            raise DatabaseConfigError("DATABASE_URL is not configured.")

        min_size = int(os.environ.get("DB_POOL_MIN_SIZE", "1"))
        max_size = int(os.environ.get("DB_POOL_MAX_SIZE", "10"))
        timeout = float(os.environ.get("DB_POOL_TIMEOUT", "10.0"))
        max_idle = float(os.environ.get("DB_POOL_MAX_IDLE", "300.0"))
        max_lifetime = float(os.environ.get("DB_POOL_MAX_LIFETIME", "3600.0"))

        try:
            pool = psycopg_pool.ConnectionPool(
                conninfo=db_url,
                min_size=min_size,
                max_size=max_size,
                timeout=timeout,
                max_idle=max_idle,
                max_lifetime=max_lifetime,
                check=psycopg_pool.ConnectionPool.check_connection,
                kwargs={"row_factory": dict_row, "autocommit": False},
                open=True,
            )
            # Test connectivity immediately to fail fast on invalid credentials
            with pool.connection(timeout=timeout) as conn:
                conn.execute("SELECT 1;")
            _pg_pool = pool
            return _pg_pool
        except Exception as e:
            masked = mask_database_url(db_url)
            raise DatabaseConnectionError(
                f"Failed to connect to PostgreSQL at {masked}: {e}"
            ) from e


def close_pg_pool() -> None:
    """Cleanly close the PostgreSQL connection pool upon application shutdown."""
    global _pg_pool
    with _pool_lock:
        if _pg_pool is not None:
            try:
                if not _pg_pool.closed:
                    _pg_pool.close()
            except Exception:
                pass
            finally:
                _pg_pool = None


def reset_pg_pool() -> None:
    """Reset the pool reference (used in testing)."""
    close_pg_pool()


def get_conn() -> Any:
    """Return a database connection (SQLite connection or pooled PostgreSQL connection).
    Prefer get_connection() context manager for automatic checkout and return.
    """
    backend = get_backend()
    if backend == "postgres":
        pool = get_pg_pool()
        return pool.getconn()
    else:
        return get_sqlite_conn()


@contextmanager
def get_connection() -> Generator[Any, None, None]:
    """Context manager that provides a connection and guarantees it is returned/closed.
    In PostgreSQL mode: checks out connection from pool and returns it on exit.
    In SQLite mode: opens connection and closes it on exit.
    """
    backend = get_backend()
    if backend == "postgres":
        pool = get_pg_pool()
        with pool.connection() as conn:
            yield conn
    else:
        conn = get_sqlite_conn()
        try:
            yield conn
        finally:
            conn.close()


def q(sqlite_query: str, postgres_query: str) -> str:
    """Return the backend-appropriate SQL statement."""
    return postgres_query if is_postgres() else sqlite_query


def execute_insert_returning_id(
    conn: Any,
    sql_sqlite: str,
    sql_postgres: str,
    params: tuple,
) -> int:
    """Execute an insert query and return the generated integer primary key ID.
    Uses cursor.lastrowid on SQLite and 'RETURNING id' on PostgreSQL.
    """
    if is_postgres():
        cursor = conn.execute(sql_postgres, params)
        row = cursor.fetchone()
        if isinstance(row, dict):
            return int(row["id"])
        return int(row[0])
    else:
        cursor = conn.execute(sql_sqlite, params)
        return int(cursor.lastrowid)


def init_workspace_schema() -> None:
    """Initialize workspace tables if they do not exist.
    Leaves the legacy requests table untouched.
    In PostgreSQL mode, DDL schema creation is deferred to G3 migrations.
    """
    if get_backend() != "sqlite":
        return

    with get_connection() as conn:
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS workspaces (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_token_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS model_slots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    workspace_id INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                    slot_number INTEGER NOT NULL CHECK(slot_number BETWEEN 1 AND 6),
                    model_name TEXT NOT NULL,
                    provider TEXT NOT NULL DEFAULT 'simulated',
                    model_id TEXT NOT NULL,
                    capability INTEGER NOT NULL CHECK(capability BETWEEN 1 AND 5),
                    context_limit INTEGER NOT NULL CHECK(context_limit > 0),
                    cost_per_1k REAL NOT NULL CHECK(cost_per_1k >= 0),
                    latency_ms INTEGER NOT NULL CHECK(latency_ms > 0),
                    failure_rate REAL NOT NULL DEFAULT 0 CHECK(failure_rate BETWEEN 0 AND 1),
                    status TEXT NOT NULL DEFAULT 'up' CHECK(status IN ('up','down')),
                    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
                    execution_mode TEXT NOT NULL DEFAULT 'simulated' CHECK(execution_mode IN ('simulated','live')),
                    api_key_encrypted TEXT NULL,
                    key_last4 TEXT NULL,
                    cost_per_1k_input REAL NULL,
                    cost_per_1k_output REAL NULL,
                    max_output_tokens INTEGER NOT NULL DEFAULT 512 CHECK(max_output_tokens BETWEEN 1 AND 4096),
                    status_reason TEXT NULL,
                    last_measured_latency_ms INTEGER NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(workspace_id, slot_number),
                    UNIQUE(workspace_id, model_name)
                );
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS model_test_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    workspace_id INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                    slot_number INTEGER NOT NULL,
                    success INTEGER NOT NULL,
                    latency_ms INTEGER NOT NULL,
                    message TEXT,
                    created_at TEXT NOT NULL
                );
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_slots_workspace
                ON model_slots(workspace_id);
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_test_runs_workspace
                ON model_test_runs(workspace_id);
                """
            )

            # Check if model_slots table needs column migrations
            slots_check = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='model_slots'"
            ).fetchone()
            if slots_check:
                col_info = conn.execute("PRAGMA table_info(model_slots)").fetchall()
                col_names = [r["name"] for r in col_info]
                if "execution_mode" not in col_names:
                    conn.execute("ALTER TABLE model_slots ADD COLUMN execution_mode TEXT NOT NULL DEFAULT 'simulated';")
                if "api_key_encrypted" not in col_names:
                    conn.execute("ALTER TABLE model_slots ADD COLUMN api_key_encrypted TEXT NULL;")
                if "key_last4" not in col_names:
                    conn.execute("ALTER TABLE model_slots ADD COLUMN key_last4 TEXT NULL;")
                if "cost_per_1k_input" not in col_names:
                    conn.execute("ALTER TABLE model_slots ADD COLUMN cost_per_1k_input REAL NULL;")
                if "cost_per_1k_output" not in col_names:
                    conn.execute("ALTER TABLE model_slots ADD COLUMN cost_per_1k_output REAL NULL;")
                if "max_output_tokens" not in col_names:
                    conn.execute("ALTER TABLE model_slots ADD COLUMN max_output_tokens INTEGER NOT NULL DEFAULT 512;")
                if "status_reason" not in col_names:
                    conn.execute("ALTER TABLE model_slots ADD COLUMN status_reason TEXT NULL;")
                if "last_measured_latency_ms" not in col_names:
                    conn.execute("ALTER TABLE model_slots ADD COLUMN last_measured_latency_ms INTEGER NULL;")

            # Check if legacy requests table exists; add workspace_id column if missing
            table_check = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='requests'"
            ).fetchone()
            if table_check:
                col_info = conn.execute("PRAGMA table_info(requests)").fetchall()
                col_names = [r["name"] for r in col_info]
                if "workspace_id" not in col_names:
                    conn.execute("ALTER TABLE requests ADD COLUMN workspace_id INTEGER;")
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_requests_workspace_id ON requests(workspace_id, id);"
                )
