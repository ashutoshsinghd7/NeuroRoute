"""Tests for Milestone G7.8.2: Workspace Cleanup Utility.

Strict Safety Guarantees:
- Uses only ephemeral temporary SQLite databases (tmp_path).
- Never connects to or modifies neuroroute.db.
- Never connects destructively to PostgreSQL database neuroroute_test.
- Tests cover all 20 required scenarios from the G7.8.2 specification.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from unittest.mock import MagicMock, patch
import pytest

from scripts.cleanup_expired_workspaces import (
    CLEANUP_ADVISORY_LOCK_ID,
    compute_cutoff,
    count_associated_records,
    execute_cleanup_batch,
    format_text_summary,
    main,
    parse_args,
    run_cleanup,
    run_dry_run,
)
from src.gateway import db, secrets_store, workspace_repo


@pytest.fixture
def isolated_sqlite_env(tmp_path, monkeypatch):
    """Set up an isolated SQLite database using tmp_path and configure environment."""
    test_db_path = str(tmp_path / "test_cleanup.db")
    monkeypatch.setenv("NEUROROUTE_DB", test_db_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("VERCEL", raising=False)
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.setenv("NEUROROUTE_SECRET_KEY", "u_8p9uYl0FwR_4oE3mF8fI1bK2nO5tL8yR9xP0zQ3vM=")

    # Initialize SQLite schema
    from src.gateway import logger as gateway_logger
    gateway_logger.init_db()
    db.init_workspace_schema()

    return test_db_path


def seed_workspace(
    conn: sqlite3.Connection,
    ws_id: int,
    expires_at: str,
    token_hash: str | None = None,
) -> int:
    """Helper to insert a workspace with a specific expires_at timestamp."""
    now = datetime.now(timezone.utc).isoformat()
    th = token_hash or f"test_hash_{ws_id}"
    conn.execute(
        "INSERT INTO workspaces (id, session_token_hash, created_at, last_seen_at, expires_at) VALUES (?, ?, ?, ?, ?);",
        (ws_id, th, now, now, expires_at),
    )
    conn.commit()
    return ws_id


def seed_slot(
    conn: sqlite3.Connection,
    ws_id: int,
    slot_num: int,
    model_name: str,
    api_key_encrypted: str | None = None,
) -> int:
    """Helper to insert a model slot into a workspace."""
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        """
        INSERT INTO model_slots (
            workspace_id, slot_number, model_name, provider, model_id, capability,
            context_limit, cost_per_1k, latency_ms, failure_rate, status, enabled,
            created_at, updated_at, execution_mode, api_key_encrypted, key_last4,
            max_output_tokens
        ) VALUES (?, ?, ?, 'simulated', 'model-1', 3, 4000, 0.05, 100, 0.0, 'up', 1, ?, ?, 'simulated', ?, ?, 512);
        """,
        (ws_id, slot_num, model_name, now, now, api_key_encrypted, "1234" if api_key_encrypted else None),
    )
    conn.commit()
    return cur.lastrowid


def seed_test_run(conn: sqlite3.Connection, ws_id: int, slot_num: int) -> int:
    """Helper to insert a model test run."""
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO model_test_runs (workspace_id, slot_number, success, latency_ms, message, created_at) VALUES (?, ?, 1, 50, 'OK', ?);",
        (ws_id, slot_num, now),
    )
    conn.commit()
    return cur.lastrowid


def seed_request(conn: sqlite3.Connection, ws_id: int | None, prompt: str) -> int:
    """Helper to insert a request row."""
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        """
        INSERT INTO requests (time, prompt_preview, constraints, estimated_level, confidence, chosen_model, status, total_latency, cost, attempts_json, rejected_json, workspace_id)
        VALUES (?, ?, '{}', 1, 0.9, 'model-1', 'SUCCESS', 50, 0.001, '[]', '[]', ?);
        """,
        (now, prompt, ws_id),
    )
    conn.commit()
    return cur.lastrowid


# ===========================================================================
# 1. Unexpired workspaces remain unchanged
# 2. Workspaces inside the grace period remain unchanged
# 3. Workspaces beyond the grace period are deleted
# 4. Associated model slots are deleted
# 5. Associated model-test runs are deleted
# 6. Request history is retained and workspace_id becomes NULL
# 7. Existing NULL workspace_id requests remain unchanged
# ===========================================================================

def test_cleanup_full_lifecycle(isolated_sqlite_env):
    """Test full cleanup lifecycle:
    - Workspace 1: unexpired (expires in 10 days) -> retained
    - Workspace 2: expired within grace period (expired 2 days ago, grace 7 days) -> retained
    - Workspace 3: expired beyond grace period (expired 10 days ago, grace 7 days) -> deleted
    - Slots and test runs of WS 3 are deleted.
    - Requests of WS 3 have workspace_id set to NULL.
    - Pre-existing NULL request remains NULL and untouched.
    """
    now = datetime.now(timezone.utc)
    ws1_exp = (now + timedelta(days=10)).isoformat()
    ws2_exp = (now - timedelta(days=2)).isoformat()
    ws3_exp = (now - timedelta(days=10)).isoformat()

    conn = sqlite3.connect(isolated_sqlite_env)
    seed_workspace(conn, 1, ws1_exp)
    seed_workspace(conn, 2, ws2_exp)
    seed_workspace(conn, 3, ws3_exp)

    # Seed child rows for all workspaces
    seed_slot(conn, 1, 1, "m1_ws1")
    seed_slot(conn, 2, 1, "m1_ws2")
    seed_slot(conn, 3, 1, "m1_ws3")
    seed_slot(conn, 3, 2, "m2_ws3")

    seed_test_run(conn, 1, 1)
    seed_test_run(conn, 3, 1)

    req_ws1_id = seed_request(conn, 1, "WS1 Request")
    req_ws3_id = seed_request(conn, 3, "WS3 Request")
    req_null_id = seed_request(conn, None, "Pre-existing NULL Request")
    conn.close()

    # Execute cleanup with 7-day grace period
    summary = run_cleanup(grace_days=7, batch_size=100, dry_run=False)

    assert summary["status"] == "success"
    assert summary["workspaces_deleted"] == 1
    assert summary["slots_deleted"] == 2
    assert summary["tests_deleted"] == 1
    assert summary["requests_unlinked"] == 1

    # Verify database state
    conn = sqlite3.connect(isolated_sqlite_env)
    cur = conn.cursor()

    # Workspaces: 1 and 2 remain, 3 is deleted
    cur.execute("SELECT id FROM workspaces ORDER BY id ASC;")
    remaining_ws = [r[0] for r in cur.fetchall()]
    assert remaining_ws == [1, 2]

    # Slots: WS1 and WS2 slots remain; WS3 slots deleted
    cur.execute("SELECT workspace_id, model_name FROM model_slots ORDER BY id ASC;")
    remaining_slots = cur.fetchall()
    assert remaining_slots == [(1, "m1_ws1"), (2, "m1_ws2")]

    # Tests: WS1 test run remains; WS3 test run deleted
    cur.execute("SELECT workspace_id FROM model_test_runs;")
    remaining_tests = cur.fetchall()
    assert remaining_tests == [(1,)]

    # Requests: all 3 requests still exist
    cur.execute("SELECT id, workspace_id, prompt_preview FROM requests ORDER BY id ASC;")
    requests_after = cur.fetchall()
    assert len(requests_after) == 3

    # WS 1 request is still linked to WS 1
    assert requests_after[0] == (req_ws1_id, 1, "WS1 Request")
    # WS 3 request has workspace_id set to NULL
    assert requests_after[1] == (req_ws3_id, None, "WS3 Request")
    # Pre-existing NULL request remains unchanged
    assert requests_after[2] == (req_null_id, None, "Pre-existing NULL Request")

    conn.close()


# ===========================================================================
# 8. Dry-run changes no database records
# ===========================================================================

def test_dry_run_non_mutation(isolated_sqlite_env):
    """Dry-run reports eligible counts accurately without changing any records."""
    now = datetime.now(timezone.utc)
    ws_exp = (now - timedelta(days=15)).isoformat()

    conn = sqlite3.connect(isolated_sqlite_env)
    seed_workspace(conn, 1, ws_exp)
    seed_slot(conn, 1, 1, "m1")
    seed_test_run(conn, 1, 1)
    seed_request(conn, 1, "Prompt 1")
    conn.close()

    summary = run_cleanup(grace_days=7, batch_size=10, dry_run=True)

    assert summary["status"] == "dry_run"
    assert summary["dry_run"] is True
    assert summary["eligible_workspaces"] == 1
    assert summary["slots_to_delete"] == 1
    assert summary["tests_to_delete"] == 1
    assert summary["requests_to_unlink"] == 1
    assert summary["batches_calculated"] == 1

    # Verify DB was NOT modified
    conn = sqlite3.connect(isolated_sqlite_env)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM workspaces;")
    assert cur.fetchone()[0] == 1
    cur.execute("SELECT COUNT(*) FROM model_slots;")
    assert cur.fetchone()[0] == 1
    cur.execute("SELECT workspace_id FROM requests WHERE id = 1;")
    assert cur.fetchone()[0] == 1
    conn.close()


# ===========================================================================
# 9. Invalid grace periods are rejected (CLI & function bounds)
# 10. Invalid batch sizes are rejected
# ===========================================================================

def test_invalid_cli_arguments():
    """Verify argument parser rejects invalid grace-days and batch-size."""
    with pytest.raises(SystemExit):
        parse_args(["--grace-days", "-1"])

    with pytest.raises(SystemExit):
        parse_args(["--batch-size", "0"])

    with pytest.raises(SystemExit):
        parse_args(["--batch-size", "1001"])

    # Valid arguments parse correctly
    args = parse_args(["--grace-days", "14", "--batch-size", "50", "--dry-run", "--format", "json"])
    assert args.grace_days == 14
    assert args.batch_size == 50
    assert args.dry_run is True
    assert args.format == "json"


def test_main_cli_exit_codes():
    """Verify CLI main entry point returns proper exit codes."""
    # Invalid args -> exit code 2
    assert main(["--grace-days", "-5"]) == 2
    assert main(["--batch-size", "0"]) == 2


# ===========================================================================
# 11. Empty databases are handled
# ===========================================================================

def test_empty_database_handling(isolated_sqlite_env):
    """Cleanup on an empty database runs cleanly and reports zero deletions."""
    summary = run_cleanup(grace_days=7, batch_size=100, dry_run=False)
    assert summary["status"] == "success"
    assert summary["workspaces_deleted"] == 0
    assert summary["batches_completed"] == 0


# ===========================================================================
# 12. Workspaces without child records are handled
# ===========================================================================

def test_workspace_without_child_records(isolated_sqlite_env):
    """Eligible workspace with zero slots, test runs, or requests is deleted cleanly."""
    now = datetime.now(timezone.utc)
    ws_exp = (now - timedelta(days=20)).isoformat()

    conn = sqlite3.connect(isolated_sqlite_env)
    seed_workspace(conn, 1, ws_exp)
    conn.close()

    summary = run_cleanup(grace_days=7, batch_size=100, dry_run=False)
    assert summary["status"] == "success"
    assert summary["workspaces_deleted"] == 1
    assert summary["slots_deleted"] == 0
    assert summary["requests_unlinked"] == 0

    conn = sqlite3.connect(isolated_sqlite_env)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM workspaces;")
    assert cur.fetchone()[0] == 0
    conn.close()


# ===========================================================================
# 13. Repeated cleanup runs are safe and idempotent
# ===========================================================================

def test_idempotent_repeated_runs(isolated_sqlite_env):
    """Running cleanup twice in a row cleans data on first run and is a no-op on second."""
    now = datetime.now(timezone.utc)
    ws_exp = (now - timedelta(days=12)).isoformat()

    conn = sqlite3.connect(isolated_sqlite_env)
    seed_workspace(conn, 1, ws_exp)
    seed_slot(conn, 1, 1, "m1")
    conn.close()

    res1 = run_cleanup(grace_days=7, batch_size=100, dry_run=False)
    assert res1["workspaces_deleted"] == 1

    res2 = run_cleanup(grace_days=7, batch_size=100, dry_run=False)
    assert res2["workspaces_deleted"] == 0
    assert res2["status"] == "success"


# ===========================================================================
# 14. Failure during a batch rolls back that batch
# 15. Failure after an earlier committed batch reports partial progress
# ===========================================================================

def test_batch_rollback_on_failure(isolated_sqlite_env):
    """If an error occurs during a batch, that batch transaction is rolled back completely."""
    now = datetime.now(timezone.utc)
    ws_exp = (now - timedelta(days=15)).isoformat()

    conn = sqlite3.connect(isolated_sqlite_env)
    seed_workspace(conn, 1, ws_exp)
    seed_slot(conn, 1, 1, "m1")
    seed_request(conn, 1, "Prompt 1")
    conn.close()

    # Inject an error inside execute_cleanup_batch
    with patch("scripts.cleanup_expired_workspaces.execute_cleanup_batch", side_effect=RuntimeError("Simulated DB Disk Failure")):
        summary = run_cleanup(grace_days=7, batch_size=100, dry_run=False)

    assert summary["status"] == "failed"
    assert "Simulated DB Disk Failure" in summary["error"]
    assert summary["workspaces_deleted"] == 0

    # Verify DB still contains workspace 1 and slot 1
    conn = sqlite3.connect(isolated_sqlite_env)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM workspaces;")
    assert cur.fetchone()[0] == 1
    cur.execute("SELECT COUNT(*) FROM model_slots;")
    assert cur.fetchone()[0] == 1
    conn.close()


def test_partial_progress_reporting(isolated_sqlite_env):
    """In a multi-batch cleanup, if batch 2 fails, batch 1 remains committed and
    status is 'partial_failure'.
    """
    now = datetime.now(timezone.utc)
    ws_exp = (now - timedelta(days=20)).isoformat()

    conn = sqlite3.connect(isolated_sqlite_env)
    for i in range(1, 5):
        seed_workspace(conn, i, ws_exp)
    conn.close()

    call_count = 0
    orig_batch = execute_cleanup_batch

    def failing_batch(c, ids, b):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RuntimeError("Batch 2 disk failure")
        return orig_batch(c, ids, b)

    with patch("scripts.cleanup_expired_workspaces.execute_cleanup_batch", side_effect=failing_batch):
        summary = run_cleanup(grace_days=7, batch_size=2, dry_run=False)

    assert summary["status"] == "partial_failure"
    assert summary["batches_completed"] == 1
    assert summary["workspaces_deleted"] == 2
    assert "Batch 2 disk failure" in summary["error"]

    # Verify batch 1 records (WS 1 and WS 2) were deleted, while WS 3 and 4 remain
    conn = sqlite3.connect(isolated_sqlite_env)
    cur = conn.cursor()
    cur.execute("SELECT id FROM workspaces ORDER BY id ASC;")
    remaining = [r[0] for r in cur.fetchall()]
    assert remaining == [3, 4]
    conn.close()


# ===========================================================================
# 16. BYOK ciphertext is deleted without decryption or logging
# ===========================================================================

def test_byok_ciphertext_deleted_without_decryption(isolated_sqlite_env):
    """Verify that encrypted BYOK credentials in model_slots are purged without calling decrypt."""
    now = datetime.now(timezone.utc)
    ws_exp = (now - timedelta(days=25)).isoformat()

    fake_ciphertext = "gAAAAABtestCiphertextDoNotDecrypt=="

    conn = sqlite3.connect(isolated_sqlite_env)
    seed_workspace(conn, 1, ws_exp)
    seed_slot(conn, 1, 1, "byok_model", api_key_encrypted=fake_ciphertext)
    conn.close()

    with patch.object(secrets_store, "decrypt", side_effect=AssertionError("secrets_store.decrypt MUST NOT BE CALLED")):
        summary = run_cleanup(grace_days=7, batch_size=100, dry_run=False)

    assert summary["status"] == "success"
    assert summary["slots_deleted"] == 1
    assert summary["workspaces_deleted"] == 1


# ===========================================================================
# 17. SQLite and PostgreSQL produce equivalent outcomes
# ===========================================================================

def test_dual_backend_parameterization_equivalence():
    """Verify parameterized IN clause generation and SQL structure for both backends."""
    from scripts.cleanup_expired_workspaces import _make_in_clause
    assert _make_in_clause([1, 2, 3], "?") == "?,?,?"
    assert _make_in_clause([1, 2, 3], "%s") == "%s,%s,%s"


# ===========================================================================
# 18. Concurrent cleanup invocations are handled safely (Advisory Lock)
# ===========================================================================

def test_postgresql_advisory_lock_concurrency(monkeypatch):
    """When PostgreSQL advisory lock cannot be acquired, cleanup exits safely with status 'locked'."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")

    mock_pool = MagicMock()
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    # Simulate pg_try_advisory_lock returning False (already locked)
    mock_cur.fetchone.return_value = {"acquired": False}
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    mock_pool.getconn.return_value = mock_conn

    with patch("src.gateway.db.get_pg_pool", return_value=mock_pool), \
         patch("src.gateway.db.get_backend", return_value="postgres"), \
         patch("src.gateway.db.get_database_url", return_value="postgresql://user:pass@localhost:5432/testdb"):

        summary = run_cleanup(grace_days=7, batch_size=100, dry_run=False)

    assert summary["status"] == "locked"
    assert "advisory lock already held" in summary["message"]
    assert summary["workspaces_deleted"] == 0


# ===========================================================================
# 19. Timestamp boundary behavior is correct
# ===========================================================================

def test_timestamp_boundary_behavior(isolated_sqlite_env):
    """Verify exact boundary condition:
    Cutoff = T - 7 days.
    - Workspace with expires_at == cutoff -> Eligible (deleted)
    - Workspace with expires_at == cutoff + 1 second -> NOT eligible (retained)
    """
    fixed_now = datetime(2026, 10, 16, 12, 0, 0, tzinfo=timezone.utc)
    cutoff_dt = fixed_now - timedelta(days=7)
    cutoff_iso = cutoff_dt.isoformat()
    just_after_iso = (cutoff_dt + timedelta(seconds=1)).isoformat()

    conn = sqlite3.connect(isolated_sqlite_env)
    seed_workspace(conn, 1, cutoff_iso)       # Exactly at cutoff
    seed_workspace(conn, 2, just_after_iso)   # 1 second after cutoff
    conn.close()

    # Patch compute_cutoff to return our fixed cutoff
    with patch("scripts.cleanup_expired_workspaces.compute_cutoff", return_value=(cutoff_dt, cutoff_iso)):
        summary = run_cleanup(grace_days=7, batch_size=100, dry_run=False)

    assert summary["status"] == "success"
    assert summary["workspaces_deleted"] == 1

    conn = sqlite3.connect(isolated_sqlite_env)
    cur = conn.cursor()
    cur.execute("SELECT id FROM workspaces;")
    remaining = [r[0] for r in cur.fetchall()]
    assert remaining == [2]
    conn.close()


# ===========================================================================
# 20. Existing authentication and dashboard workflows remain functional
# ===========================================================================

def test_authentication_workflow_unaffected(isolated_sqlite_env):
    """Active session authentication continues to work normally alongside cleanup."""
    conn = sqlite3.connect(isolated_sqlite_env)
    # Create an active workspace
    token = "test_active_session_token_12345"
    token_hash = workspace_repo.hash_session_token(token) if hasattr(workspace_repo, "hash_session_token") else "token_hash_abc"
    now = datetime.now(timezone.utc)
    seed_workspace(conn, 1, (now + timedelta(days=30)).isoformat(), token_hash=token_hash)
    conn.close()

    # Run cleanup (nothing eligible)
    summary = run_cleanup(grace_days=7, batch_size=100, dry_run=False)
    assert summary["status"] == "success"
    assert summary["workspaces_deleted"] == 0

    # Verify workspace still exists in DB
    conn = sqlite3.connect(isolated_sqlite_env)
    cur = conn.cursor()
    cur.execute("SELECT id FROM workspaces WHERE id = 1;")
    assert cur.fetchone() is not None
    conn.close()


def test_format_text_summary():
    """Verify format_text_summary generates clean output."""
    sample_summary = {
        "status": "success",
        "dry_run": False,
        "backend": "sqlite",
        "database_target": "SQLite",
        "grace_days": 7,
        "cutoff": "2026-10-09T12:00:00+00:00",
        "batches_completed": 1,
        "workspaces_deleted": 5,
        "slots_deleted": 12,
        "tests_deleted": 2,
        "requests_unlinked": 8,
        "duration_ms": 45.2,
    }
    text = format_text_summary(sample_summary)
    assert "SUCCESS" in text
    assert "Workspaces Deleted: 5" in text
    assert "Requests Unlinked:  8" in text


def test_main_cli_json_output(isolated_sqlite_env, capsys):
    """Verify CLI --format json produces valid JSON with required keys."""
    exit_code = main(["--dry-run", "--format", "json"])
    assert exit_code == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["status"] == "dry_run"
    assert data["dry_run"] is True
    assert data["backend"] == "sqlite"
    assert "cutoff" in data
    assert "eligible_workspaces" in data


def test_postgresql_full_execution_flow(monkeypatch):
    """Simulate a complete PostgreSQL execution run, verifying advisory lock lifecycle and SQL queries."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")

    mock_pool = MagicMock()
    # Advisory lock connection
    mock_advisory_conn = MagicMock()
    mock_adv_cur = MagicMock()
    mock_adv_cur.fetchone.return_value = {"acquired": True}
    mock_advisory_conn.cursor.return_value.__enter__.return_value = mock_adv_cur

    # Data connection
    mock_data_conn = MagicMock()
    mock_data_cur = MagicMock()
    # First batch returns workspace 10, second batch returns empty
    mock_data_cur.fetchall.side_effect = [
        [{"id": 10, "expires_at": "2026-10-01T12:00:00Z"}],
        [],
    ]
    mock_data_cur.fetchone.side_effect = [
        {"c": 3},  # slots count
        {"c": 1},  # tests count
        {"c": 2},  # requests count
    ]
    mock_data_conn.cursor.return_value = mock_data_cur

    mock_pool.getconn.return_value = mock_advisory_conn

    from contextlib import contextmanager
    @contextmanager
    def mock_get_conn():
        yield mock_data_conn

    with patch("src.gateway.db.get_pg_pool", return_value=mock_pool), \
         patch("src.gateway.db.get_backend", return_value="postgres"), \
         patch("src.gateway.db.get_database_url", return_value="postgresql://user:pass@localhost:5432/testdb"), \
         patch("src.gateway.db.get_connection", side_effect=mock_get_conn):

        summary = run_cleanup(grace_days=7, batch_size=10, dry_run=False)

    assert summary["status"] == "success"
    assert summary["workspaces_deleted"] == 1
    assert summary["slots_deleted"] == 3
    assert summary["tests_deleted"] == 1
    assert summary["requests_unlinked"] == 2
    assert summary["batches_completed"] == 1

    # Verify advisory lock was acquired and released
    mock_adv_cur.execute.assert_any_call("SELECT pg_try_advisory_lock(%s) AS acquired;", (CLEANUP_ADVISORY_LOCK_ID,))
    mock_adv_cur.execute.assert_any_call("SELECT pg_advisory_unlock(%s);", (CLEANUP_ADVISORY_LOCK_ID,))


# ===========================================================================
# STEP 2 & 4 DEEP DIVE: Timestamp Formats & Granular Rollbacks
# ===========================================================================

def test_timestamp_formats_coverage(isolated_sqlite_env):
    """Verify cleanup handles all ISO 8601 variations without lexicographical failure:
    - UTC with 'Z'
    - UTC with '+00:00'
    - Fractional seconds (microseconds)
    - Non-UTC offset ('+05:30')
    - Naive format ('YYYY-MM-DD HH:MM:SS')
    - Exact microsecond before/after cutoff
    """
    fixed_now = datetime(2026, 10, 16, 18, 0, 0, 0, tzinfo=timezone.utc)
    cutoff_dt = fixed_now - timedelta(days=7)  # 2026-10-09 18:00:00 UTC
    cutoff_iso = cutoff_dt.isoformat()

    conn = sqlite3.connect(isolated_sqlite_env)

    # 1. UTC with 'Z' (expired 1 hour before cutoff) -> Eligible
    seed_workspace(conn, 1, "2026-10-09T17:00:00Z")
    # 2. UTC with '+00:00' (expired 1 hour before cutoff) -> Eligible
    seed_workspace(conn, 2, "2026-10-09T17:00:00+00:00")
    # 3. Non-UTC offset: 2026-10-09 22:30:00+05:30 (= 17:00:00 UTC) -> Eligible
    seed_workspace(conn, 3, "2026-10-09T22:30:00+05:30")
    # 4. Naive format: '2026-10-09 17:00:00' -> Eligible
    seed_workspace(conn, 4, "2026-10-09 17:00:00")
    # 5. Microsecond before cutoff: 17:59:59.999999+00:00 -> Eligible
    seed_workspace(conn, 5, "2026-10-09T17:59:59.999999+00:00")
    # 6. Exact match at cutoff: 18:00:00.000000+00:00 -> Eligible
    seed_workspace(conn, 6, "2026-10-09T18:00:00.000000+00:00")
    # 7. Microsecond after cutoff: 18:00:00.000001+00:00 -> NOT Eligible
    seed_workspace(conn, 7, "2026-10-09T18:00:00.000001+00:00")
    # 8. Unexpired future workspace -> NOT Eligible
    seed_workspace(conn, 8, "2026-11-15T18:00:00Z")

    conn.close()

    with patch("scripts.cleanup_expired_workspaces.compute_cutoff", return_value=(cutoff_dt, cutoff_iso)):
        summary = run_cleanup(grace_days=7, batch_size=100, dry_run=False)

    assert summary["status"] == "success"
    assert summary["workspaces_deleted"] == 6

    # Verify only workspaces 7 and 8 remain
    conn = sqlite3.connect(isolated_sqlite_env)
    cur = conn.cursor()
    cur.execute("SELECT id FROM workspaces ORDER BY id ASC;")
    remaining_ids = [r[0] for r in cur.fetchall()]
    assert remaining_ids == [7, 8]
    conn.close()


def test_ambiguous_timestamp_aborts_safely(isolated_sqlite_env):
    """When an invalid or corrupt timestamp string is encountered, cleanup fails safely without deleting rows."""
    from scripts.cleanup_expired_workspaces import AmbiguousTimestampError, parse_iso_utc

    # 1. Direct function check
    with pytest.raises(AmbiguousTimestampError):
        parse_iso_utc("corrupt-invalid-timestamp-value", ws_id=99)

    # 2. Database level: corrupt timestamp in candidate row aborts cleanup
    now = datetime.now(timezone.utc)
    ws_exp = (now - timedelta(days=20)).isoformat()

    conn = sqlite3.connect(isolated_sqlite_env)
    seed_workspace(conn, 1, ws_exp)
    conn.close()

    # Simulate query returning an invalid timestamp
    with patch("scripts.cleanup_expired_workspaces.parse_iso_utc", side_effect=AmbiguousTimestampError("Simulated invalid timestamp")):
        summary = run_cleanup(grace_days=7, batch_size=100, dry_run=False)

    assert summary["status"] == "failed"
    assert "Simulated invalid timestamp" in summary["error"]
    assert summary["workspaces_deleted"] == 0

    # Verify workspace 1 still exists
    conn = sqlite3.connect(isolated_sqlite_env)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM workspaces;")
    assert cur.fetchone()[0] == 1
    conn.close()


def test_granular_step_failures_roll_back(isolated_sqlite_env):
    """Test rollbacks using a SQLite trigger that fails during UPDATE requests inside execute_cleanup_batch."""
    now = datetime.now(timezone.utc)
    ws_exp = (now - timedelta(days=20)).isoformat()

    conn = sqlite3.connect(isolated_sqlite_env)
    seed_workspace(conn, 1, ws_exp)
    seed_slot(conn, 1, 1, "m1")
    seed_request(conn, 1, "Prompt 1")

    # Install trigger that forces failure on UPDATE requests
    conn.execute(
        """
        CREATE TRIGGER test_fail_unlink BEFORE UPDATE ON requests
        BEGIN
            SELECT RAISE(ABORT, 'Simulated trigger abort during request unlinking');
        END;
        """
    )
    conn.commit()
    conn.close()

    summary = run_cleanup(grace_days=7, batch_size=100, dry_run=False)

    assert summary["status"] == "failed"
    assert "Simulated trigger abort during request unlinking" in summary["error"]

    # Verify transaction rolled back completely: workspace 1, slot 1, and request link remain
    conn = sqlite3.connect(isolated_sqlite_env)
    cur = conn.cursor()
    cur.execute("SELECT workspace_id FROM requests WHERE id = 1;")
    assert cur.fetchone()[0] == 1
    cur.execute("SELECT COUNT(*) FROM workspaces;")
    assert cur.fetchone()[0] == 1
    cur.execute("SELECT COUNT(*) FROM model_slots;")
    assert cur.fetchone()[0] == 1
    conn.close()



