"""Tests for Milestone G4: SQLite to PostgreSQL Data Migration and Verification.

Covers:
1. Source database inspection and integrity validation.
2. Target schema readiness and empty-state safety guards.
3. Dry-run preflight mode (no data written to destination).
4. Dependency-ordered data migration (workspaces -> slots -> test_runs -> requests).
5. Primary key ID preservation with OVERRIDING SYSTEM VALUE.
6. Nullable legacy request workspace_id preservation.
7. Encrypted API key ciphertext preservation without decryption or exposure.
8. Single-transaction rollback on failure.
9. Identity sequence synchronization and collision prevention.
10. Post-migration verification of 100% data parity and error detection.
11. Credential-safe error sanitization.
12. Idempotency and repeat execution protection.
"""
from datetime import datetime, timezone
import os
import sqlite3
from unittest.mock import MagicMock, patch
import pytest

from scripts.migrate_sqlite_to_postgres import (
    TABLES_IN_ORDER,
    MigrationError,
    MigrationVerificationError,
    SourceDatabaseError,
    TargetNotEmptyError,
    TargetSchemaError,
    inspect_source_database,
    mask_url,
    migrate_data,
    sanitize_message,
    sync_identity_sequences,
    validate_target_database,
    verify_migration,
)


@pytest.fixture
def mock_sqlite_db(tmp_path):
    """Create a temporary SQLite database populated with representative NeuroRoute data."""
    db_file = str(tmp_path / "mock_neuroroute.db")
    conn = sqlite3.connect(db_file)
    conn.execute(
        """
        CREATE TABLE workspaces (
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
        CREATE TABLE model_slots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workspace_id INTEGER NOT NULL REFERENCES workspaces(id),
            slot_number INTEGER NOT NULL,
            model_name TEXT NOT NULL,
            provider TEXT NOT NULL,
            model_id TEXT NOT NULL,
            capability INTEGER NOT NULL,
            context_limit INTEGER NOT NULL,
            cost_per_1k REAL NOT NULL,
            latency_ms INTEGER NOT NULL,
            failure_rate REAL NOT NULL,
            status TEXT NOT NULL,
            enabled INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            execution_mode TEXT NOT NULL,
            api_key_encrypted TEXT,
            key_last4 TEXT,
            cost_per_1k_input REAL,
            cost_per_1k_output REAL,
            max_output_tokens INTEGER NOT NULL,
            status_reason TEXT,
            last_measured_latency_ms INTEGER
        );
        """
    )
    conn.execute(
        """
        CREATE TABLE model_test_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workspace_id INTEGER NOT NULL REFERENCES workspaces(id),
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
        CREATE TABLE requests (
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
            rejected_json TEXT,
            workspace_id INTEGER REFERENCES workspaces(id)
        );
        """
    )

    now = datetime(2026, 10, 14, 12, 0, 0, tzinfo=timezone.utc).isoformat()

    # Populate 2 workspaces
    conn.execute(
        "INSERT INTO workspaces (id, session_token_hash, created_at, last_seen_at, expires_at) VALUES (?, ?, ?, ?, ?);",
        (1, "hash_ws1", now, now, now),
    )
    conn.execute(
        "INSERT INTO workspaces (id, session_token_hash, created_at, last_seen_at, expires_at) VALUES (?, ?, ?, ?, ?);",
        (2, "hash_ws2", now, now, now),
    )

    # Populate slots (including an encrypted BYOK slot with raw ciphertext)
    conn.execute(
        """
        INSERT INTO model_slots (
            id, workspace_id, slot_number, model_name, provider, model_id, capability,
            context_limit, cost_per_1k, latency_ms, failure_rate, status, enabled,
            created_at, updated_at, execution_mode, api_key_encrypted, key_last4,
            max_output_tokens
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """,
        (10, 1, 1, "m1", "simulated", "sim-1", 3, 4000, 0.05, 100, 0.0, "up", 1, now, now, "simulated", None, None, 512),
    )
    conn.execute(
        """
        INSERT INTO model_slots (
            id, workspace_id, slot_number, model_name, provider, model_id, capability,
            context_limit, cost_per_1k, latency_ms, failure_rate, status, enabled,
            created_at, updated_at, execution_mode, api_key_encrypted, key_last4,
            max_output_tokens
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """,
        (
            20, 1, 2, "m2_byok", "groq", "llama-3.3-70b", 4, 8000, 0.10, 150, 0.0,
            "up", 1, now, now, "live", "gAAAAABnh123OpaqueCiphertextABC==", "9999", 1024
        ),
    )

    # Populate 1 test run
    conn.execute(
        "INSERT INTO model_test_runs (id, workspace_id, slot_number, success, latency_ms, message, created_at) VALUES (?, ?, ?, ?, ?, ?, ?);",
        (5, 1, 1, 1, 120, "Success", now),
    )

    # Populate 2 requests: 1 legacy row with workspace_id=NULL, 1 with workspace_id=1
    conn.execute(
        """
        INSERT INTO requests (id, time, prompt_preview, constraints, estimated_level, confidence, chosen_model, status, total_latency, cost, attempts_json, rejected_json, workspace_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """,
        (100, "12:00:00", "Legacy prompt", "{}", 1, 0.9, "m1", "SUCCESS", 110, 0.01, "[]", "[]", None),
    )
    conn.execute(
        """
        INSERT INTO requests (id, time, prompt_preview, constraints, estimated_level, confidence, chosen_model, status, total_latency, cost, attempts_json, rejected_json, workspace_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """,
        (101, "12:05:00", "Workspace prompt", "{}", 2, 0.95, "m1", "SUCCESS", 115, 0.02, "[]", "[]", 1),
    )

    conn.commit()
    conn.close()
    return db_file


# ---------------------------------------------------------------------------
# 1. Source Database Validation Tests
# ---------------------------------------------------------------------------

def test_inspect_source_database_success(mock_sqlite_db):
    """inspect_source_database correctly verifies integrity and counts rows."""
    info = inspect_source_database(mock_sqlite_db)
    assert info["counts"]["workspaces"] == 2
    assert info["counts"]["model_slots"] == 2
    assert info["counts"]["model_test_runs"] == 1
    assert info["counts"]["requests"] == 2


def test_inspect_source_database_nonexistent():
    """Non-existent SQLite file raises SourceDatabaseError."""
    with pytest.raises(SourceDatabaseError, match="does not exist"):
        inspect_source_database("non_existent_file.db")


def test_inspect_source_database_missing_tables(tmp_path):
    """SQLite database missing required tables raises SourceDatabaseError."""
    empty_db = str(tmp_path / "empty.db")
    conn = sqlite3.connect(empty_db)
    conn.execute("CREATE TABLE dummy (id int);")
    conn.close()

    with pytest.raises(SourceDatabaseError, match="missing required tables"):
        inspect_source_database(empty_db)


# ---------------------------------------------------------------------------
# 2. Target Safety and Empty-State Checks
# ---------------------------------------------------------------------------

def test_validate_target_database_missing_tables():
    """Target PostgreSQL missing tables raises TargetSchemaError."""
    mock_conn = MagicMock()
    mock_conn.execute.return_value.fetchall.return_value = [{"table_name": "other"}]

    with pytest.raises(TargetSchemaError, match="missing required tables"):
        validate_target_database(mock_conn)


def test_validate_target_database_not_empty_raises():
    """Target PostgreSQL containing pre-existing rows raises TargetNotEmptyError."""
    mock_conn = MagicMock()
    # First query for information_schema returns all 4 tables
    def mock_execute(sql, params=None):
        m = MagicMock()
        if "information_schema.tables" in sql:
            m.fetchall.return_value = [{"table_name": t} for t in TABLES_IN_ORDER]
        elif "FROM workspaces" in sql:
            m.fetchone.return_value = {"count": 5}
        else:
            m.fetchone.return_value = {"count": 0}
        return m

    mock_conn.execute.side_effect = mock_execute

    with pytest.raises(TargetNotEmptyError, match="Target PostgreSQL database is NOT empty"):
        validate_target_database(mock_conn)


# ---------------------------------------------------------------------------
# 3. Dry-Run Behavior
# ---------------------------------------------------------------------------

def test_dry_run_executes_no_writes(mock_sqlite_db):
    """Dry-run validates schema and counts without executing INSERT statements."""
    mock_conn = MagicMock()
    def mock_execute(sql, params=None):
        m = MagicMock()
        if "information_schema.tables" in sql:
            m.fetchall.return_value = [{"table_name": t} for t in TABLES_IN_ORDER]
        elif "count(*)" in sql:
            m.fetchone.return_value = {"count": 0}
        return m
    mock_conn.execute.side_effect = mock_execute

    summary = migrate_data(mock_sqlite_db, mock_conn, dry_run=True)
    assert summary["status"] == "DRY_RUN_PASSED"
    assert summary["dry_run"] is True

    # Assert no INSERT statements were called
    for call in mock_conn.execute.call_args_list:
        sql = call[0][0]
        assert "INSERT INTO" not in sql


# ---------------------------------------------------------------------------
# 4. Data Migration, ID Preservation, and Ordering
# ---------------------------------------------------------------------------

def test_migrate_data_topological_order_and_overriding_system_value(mock_sqlite_db):
    """Verify tables are migrated in parent-before-child order using OVERRIDING SYSTEM VALUE."""
    executed_statements = []
    mock_conn = MagicMock()

    # Context manager returns self
    mock_conn.__enter__.return_value = mock_conn

    def mock_execute(sql, params=None):
        executed_statements.append((sql.strip(), params))
        m = MagicMock()
        if "information_schema.tables" in sql:
            m.fetchall.return_value = [{"table_name": t} for t in TABLES_IN_ORDER]
        elif "count(*)" in sql:
            m.fetchone.return_value = {"count": 0}
        elif "MAX(id)" in sql:
            m.fetchone.return_value = {"max_id": 100}
        elif "SELECT *" in sql:
            # For verification step, mock returns matching rows
            if "FROM workspaces" in sql:
                m.fetchall.return_value = [
                    {"id": 1, "session_token_hash": "hash_ws1"},
                    {"id": 2, "session_token_hash": "hash_ws2"},
                ]
            elif "FROM model_slots" in sql:
                m.fetchall.return_value = [
                    {"id": 10, "workspace_id": 1, "slot_number": 1, "model_name": "m1", "provider": "simulated",
                     "model_id": "sim-1", "capability": 3, "status": "up", "enabled": 1, "execution_mode": "simulated",
                     "api_key_encrypted": None, "key_last4": None},
                    {"id": 20, "workspace_id": 1, "slot_number": 2, "model_name": "m2_byok", "provider": "groq",
                     "model_id": "llama-3.3-70b", "capability": 4, "status": "up", "enabled": 1, "execution_mode": "live",
                     "api_key_encrypted": "gAAAAABnh123OpaqueCiphertextABC==", "key_last4": "9999"},
                ]
            elif "FROM model_test_runs" in sql:
                m.fetchall.return_value = [
                    {"id": 5, "workspace_id": 1, "slot_number": 1, "success": 1, "latency_ms": 120},
                ]
            elif "FROM requests" in sql:
                m.fetchall.return_value = [
                    {"id": 100, "workspace_id": None, "time": "12:00:00", "prompt_preview": "Legacy prompt",
                     "constraints": "{}", "status": "SUCCESS", "attempts_json": "[]", "rejected_json": "[]"},
                    {"id": 101, "workspace_id": 1, "time": "12:05:00", "prompt_preview": "Workspace prompt",
                     "constraints": "{}", "status": "SUCCESS", "attempts_json": "[]", "rejected_json": "[]"},
                ]
        return m

    mock_conn.execute.side_effect = mock_execute

    summary = migrate_data(mock_sqlite_db, mock_conn, dry_run=False)
    assert summary["status"] == "MIGRATION_SUCCESSFUL_AND_VERIFIED"
    assert summary["migrated"]["workspaces"] == 2
    assert summary["migrated"]["model_slots"] == 2
    assert summary["migrated"]["model_test_runs"] == 1
    assert summary["migrated"]["requests"] == 2

    # Verify OVERRIDING SYSTEM VALUE is in every INSERT
    inserts = [stmt for stmt, _ in executed_statements if "INSERT INTO" in stmt]
    assert len(inserts) == 7  # 2 ws + 2 slots + 1 test + 2 reqs
    for ins in inserts:
        assert "OVERRIDING SYSTEM VALUE" in ins

    # Verify insertion order: workspaces before slots before test_runs before requests
    table_order = []
    for ins in inserts:
        for t in TABLES_IN_ORDER:
            if f"INSERT INTO {t}" in ins:
                if not table_order or table_order[-1] != t:
                    table_order.append(t)
    assert table_order == TABLES_IN_ORDER

    # Verify ciphertext was passed unmodified without logging
    byok_insert = [stmt for stmt, p in executed_statements if p and "gAAAAABnh123OpaqueCiphertextABC==" in p]
    assert len(byok_insert) == 1


# ---------------------------------------------------------------------------
# 5. Identity Sequence Synchronization Tests
# ---------------------------------------------------------------------------

def test_sync_identity_sequences():
    """sync_identity_sequences resets sequence to MAX(id) + 1 for populated tables and 1 for empty tables."""
    executed = []
    mock_conn = MagicMock()

    def mock_execute(sql, params=None):
        executed.append((sql.strip(), params))
        m = MagicMock()
        if "FROM workspaces" in sql:
            m.fetchone.return_value = {"max_id": 9}
        elif "FROM model_slots" in sql:
            m.fetchone.return_value = {"max_id": 24}
        elif "FROM model_test_runs" in sql:
            m.fetchone.return_value = {"max_id": None}  # Empty table
        elif "FROM requests" in sql:
            m.fetchone.return_value = {"max_id": 7}
        return m

    mock_conn.execute.side_effect = mock_execute

    synced = sync_identity_sequences(mock_conn)
    assert synced["workspaces"] == 10
    assert synced["model_slots"] == 25
    assert synced["model_test_runs"] == 1
    assert synced["requests"] == 8

    # Verify setval calls
    setvals = [s for s, p in executed if "SELECT setval" in s]
    assert len(setvals) == 4


# ---------------------------------------------------------------------------
# 6. Verification and Error Detection Tests
# ---------------------------------------------------------------------------

def test_verify_migration_count_mismatch_raises(mock_sqlite_db):
    """Row count mismatch raises MigrationVerificationError."""
    mock_conn = MagicMock()
    # Return 1 workspace instead of 2
    mock_conn.execute.return_value.fetchall.return_value = [
        {"id": 1, "session_token_hash": "hash_ws1"}
    ]

    with pytest.raises(MigrationVerificationError, match="Workspaces count mismatch"):
        verify_migration(mock_sqlite_db, mock_conn)


def test_verify_migration_ciphertext_mismatch_raises(mock_sqlite_db):
    """Ciphertext alteration raises MigrationVerificationError."""
    mock_conn = MagicMock()

    def mock_execute(sql, params=None):
        m = MagicMock()
        if "FROM workspaces" in sql:
            m.fetchall.return_value = [
                {"id": 1, "session_token_hash": "hash_ws1"},
                {"id": 2, "session_token_hash": "hash_ws2"},
            ]
        elif "FROM model_slots" in sql:
            # Corrupt the ciphertext
            m.fetchall.return_value = [
                {"id": 10, "workspace_id": 1, "slot_number": 1, "model_name": "m1", "provider": "simulated",
                 "model_id": "sim-1", "capability": 3, "status": "up", "enabled": 1, "execution_mode": "simulated",
                 "api_key_encrypted": None, "key_last4": None},
                {"id": 20, "workspace_id": 1, "slot_number": 2, "model_name": "m2_byok", "provider": "groq",
                 "model_id": "llama-3.3-70b", "capability": 4, "status": "up", "enabled": 1, "execution_mode": "live",
                 "api_key_encrypted": "DIFFERENT_CIPHERTEXT", "key_last4": "9999"},
            ]
        return m

    mock_conn.execute.side_effect = mock_execute

    with pytest.raises(MigrationVerificationError, match="api_key_encrypted ciphertext mismatch"):
        verify_migration(mock_sqlite_db, mock_conn)


# ---------------------------------------------------------------------------
# 7. Transaction Rollback on Failure
# ---------------------------------------------------------------------------

def test_migration_failure_triggers_transaction_rollback(mock_sqlite_db):
    """Simulated database error triggers transaction abort without partial commit."""
    mock_conn = MagicMock()
    mock_conn.__enter__.return_value = mock_conn

    def mock_execute(sql, params=None):
        if "information_schema.tables" in sql:
            m = MagicMock()
            m.fetchall.return_value = [{"table_name": t} for t in TABLES_IN_ORDER]
            return m
        if "count(*)" in sql:
            m = MagicMock()
            m.fetchone.return_value = {"count": 0}
            return m
        if "INSERT INTO model_slots" in sql:
            raise RuntimeError("PostgreSQL Disk Full Error")
        return MagicMock()

    mock_conn.execute.side_effect = mock_execute

    with pytest.raises(RuntimeError, match="PostgreSQL Disk Full Error"):
        migrate_data(mock_sqlite_db, mock_conn)

    # Verify context manager exited with exception (rollback triggered)
    assert mock_conn.transaction().__exit__.called or mock_conn.__exit__.called


# ---------------------------------------------------------------------------
# 8. Credential Masking in Utility
# ---------------------------------------------------------------------------

def test_mask_url_and_sanitize_message():
    """mask_url and sanitize_message redact credentials from logs and URLs."""
    raw_url = "postgresql://user:top_secret_pass@db.cloud.io:5432/nr_prod?sslmode=require"
    masked = mask_url(raw_url)
    assert "top_secret_pass" not in masked
    assert "user:***@db.cloud.io:5432/nr_prod" in masked

    err = f"Failed to connect to {raw_url} due to authentication error with top_secret_pass"
    sanitized = sanitize_message(err, raw_url)
    assert "top_secret_pass" not in sanitized
    assert "[REDACTED_PASSWORD]" in sanitized or "[REDACTED_URL]" in sanitized


# ---------------------------------------------------------------------------
# 9. Dialect Verification and Schema Initialization Tests
# ---------------------------------------------------------------------------

def test_validate_target_database_rejects_non_postgres():
    """validate_target_database raises TargetSchemaError if target engine is not PostgreSQL."""
    mock_conn = MagicMock()
    mock_conn.execute.return_value.fetchone.return_value = {"version": "MySQL 8.0.32"}

    with pytest.raises(TargetSchemaError, match="Target connection is not PostgreSQL"):
        validate_target_database(mock_conn)


def test_migrate_postgres_preserves_caller_connection_open(tmp_path):
    """Verify migrate_postgres uses transaction context manager and leaves caller connection open."""
    from src.gateway.migrator import migrate_postgres
    test_mig_dir = str(tmp_path / "open_conn_mig")
    os.makedirs(test_mig_dir, exist_ok=True)
    with open(os.path.join(test_mig_dir, "001_dummy.sql"), "w") as f:
        f.write("CREATE TABLE dummy_test (id int);")

    closed = False

    class FakePsycopgConnection:
        def __init__(self):
            self.closed = False
        def close(self):
            self.closed = True
        def transaction(self):
            class Tx:
                def __enter__(inner_self): return inner_self
                def __exit__(inner_self, *args): pass
            return Tx()
        def execute(self, sql, params=None):
            class C:
                def fetchall(self): return []
                def fetchone(self): return None
            return C()

    conn = FakePsycopgConnection()
    migrate_postgres(conn=conn, directory=test_mig_dir)

    # In Psycopg 3, with conn: would have called close(). With .transaction(), close() is NOT called!
    assert conn.closed is False
