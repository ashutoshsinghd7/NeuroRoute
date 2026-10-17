#!/usr/bin/env python3
"""NeuroRoute Workspace Expiration Cleanup Utility (Milestone G7.8.2).

Safely, idempotently, and atomically purges workspaces that have expired beyond
the configured grace period, along with their associated child model slots and test runs.
Unlinks request history by setting requests.workspace_id to NULL to preserve analytics.

Strict Safety Guarantees:
1. Isolation: Operates only on configured backend via src.gateway.db abstraction.
2. Cutoff Rule: Only deletes workspaces where expires_at <= (now_utc - grace_period).
3. Secret Safety: NEVER reads, logs, or decrypts api_key_encrypted ciphertext.
4. History Preservation: Anonymizes requests by setting workspace_id = NULL. Unrelated
   NULL workspace_id requests are preserved untouched.
5. Dual-Backend Consistency: Explicitly unlinks requests and deletes child records
   without relying on SQLite foreign-key cascades being active.
6. Per-Batch Atomicity: Each batch executes in its own isolated transaction with
   safe rollback on error, bounded lock durations, and incremental progress.
7. Concurrency Safety:
   - PostgreSQL: Session-level advisory lock prevents multiple concurrent runs.
   - SQLite: BEGIN IMMEDIATE write transactions with bounded busy timeouts.
8. Non-Destructive Dry-Run: Validates and counts eligible workspaces and child records
   without altering any database rows.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

# Ensure project root is in sys.path when executed directly
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from src.gateway import db

# Advisory lock ID for PostgreSQL workspace cleanup (32-bit positive integer)
CLEANUP_ADVISORY_LOCK_ID = 8492026

logger = logging.getLogger("neuroroute.cleanup")


class CleanupError(Exception):
    """Base exception for cleanup errors."""
    pass


class ConcurrencyLockError(CleanupError):
    """Raised when a concurrent cleanup operation holds the lock."""
    pass


class AmbiguousTimestampError(CleanupError):
    """Raised when an unparseable or ambiguous timestamp is encountered."""
    pass


def parse_iso_utc(val: Any, ws_id: Optional[int] = None) -> datetime:
    """Parse and normalize any timestamp into a timezone-aware UTC datetime.
    Handles ISO 8601 strings with Z, +00:00, or any valid timezone offset,
    with or without fractional seconds.
    Fails safely if the timestamp cannot be unambiguously parsed.
    """
    if isinstance(val, datetime):
        if val.tzinfo is None:
            return val.replace(tzinfo=timezone.utc)
        return val.astimezone(timezone.utc)

    s = str(val).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"

    try:
        dt = datetime.fromisoformat(s)
    except Exception as exc:
        ws_msg = f" for workspace ID {ws_id}" if ws_id is not None else ""
        raise AmbiguousTimestampError(
            f"Unparseable or ambiguous timestamp '{val}'{ws_msg}: {exc}. "
            "Aborting cleanup to prevent premature deletion."
        ) from exc

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """Parse and validate CLI arguments before establishing database connections."""
    parser = argparse.ArgumentParser(
        description="NeuroRoute expired workspace cleanup utility.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate cleanup without modifying or deleting database records.",
    )
    parser.add_argument(
        "--grace-days",
        type=int,
        default=7,
        help="Grace period in days beyond expires_at before a workspace is eligible for deletion.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="Maximum number of workspaces processed per transaction batch (1 to 1000).",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format for execution summary.",
    )
    parser.add_argument(
        "--db-url",
        type=str,
        default=None,
        help="Optional database connection URL (overrides CLEANUP_DATABASE_URL and DATABASE_URL).",
    )

    args = parser.parse_args(argv)

    # Validate argument bounds before touching the database
    if args.grace_days < 0:
        parser.error(f"--grace-days must be non-negative (got {args.grace_days}).")
    if args.batch_size < 1 or args.batch_size > 1000:
        parser.error(f"--batch-size must be between 1 and 1000 (got {args.batch_size}).")

    return args


def compute_cutoff(grace_days: int) -> Tuple[datetime, str]:
    """Calculate the single UTC cutoff datetime and ISO-8601 string for this cleanup run.
    A workspace is eligible only when expires_at <= cutoff.
    """
    now_utc = datetime.now(timezone.utc)
    cutoff_dt = now_utc - timedelta(days=grace_days)
    return cutoff_dt, cutoff_dt.isoformat()


def _make_in_clause(ids: List[int], placeholder: str) -> str:
    """Generate parameterized IN (?, ?, ...) or IN (%s, %s, ...) clause."""
    return ",".join([placeholder] * len(ids))


def count_associated_records(
    conn: Any,
    workspace_ids: List[int],
    backend: str,
) -> Tuple[int, int, int]:
    """Count associated model_slots, model_test_runs, and requests for the given workspaces.
    Does NOT retrieve any column data or decrypted secrets.
    """
    if not workspace_ids:
        return 0, 0, 0

    placeholder = "%s" if backend == "postgres" else "?"
    in_clause = _make_in_clause(workspace_ids, placeholder)

    sql_slots = f"SELECT COUNT(*) AS c FROM model_slots WHERE workspace_id IN ({in_clause})"
    sql_tests = f"SELECT COUNT(*) AS c FROM model_test_runs WHERE workspace_id IN ({in_clause})"
    sql_requests = f"SELECT COUNT(*) AS c FROM requests WHERE workspace_id IN ({in_clause})"

    cur = conn.cursor()
    cur.execute(sql_slots, tuple(workspace_ids))
    slots_res = cur.fetchone()
    slots_count = slots_res["c"] if hasattr(slots_res, "keys") else slots_res[0]

    cur.execute(sql_tests, tuple(workspace_ids))
    tests_res = cur.fetchone()
    tests_count = tests_res["c"] if hasattr(tests_res, "keys") else tests_res[0]

    cur.execute(sql_requests, tuple(workspace_ids))
    reqs_res = cur.fetchone()
    requests_count = reqs_res["c"] if hasattr(reqs_res, "keys") else reqs_res[0]

    return int(slots_count), int(tests_count), int(requests_count)


def run_dry_run(
    conn: Any,
    backend: str,
    cutoff_dt: datetime,
    cutoff_iso: str,
    batch_size: int,
) -> Dict[str, Any]:
    """Perform a read-only dry run calculating eligible workspaces and child records."""
    cur = conn.cursor()
    if backend == "postgres":
        sql_select = "SELECT id, expires_at FROM workspaces WHERE expires_at <= %s ORDER BY id ASC"
        cur.execute(sql_select, (cutoff_iso,))
    else:
        sql_select = (
            "SELECT id, expires_at FROM workspaces "
            "WHERE strftime('%Y-%m-%d %H:%M:%f', expires_at) <= strftime('%Y-%m-%d %H:%M:%f', ?) "
            "ORDER BY id ASC"
        )
        cur.execute(sql_select, (cutoff_iso,))

    all_rows = cur.fetchall()

    eligible_ids = []
    for row in all_rows:
        row_id = row["id"] if hasattr(row, "keys") else row[0]
        exp_val = row["expires_at"] if hasattr(row, "keys") else row[1]
        dt = parse_iso_utc(exp_val, ws_id=row_id)
        if dt <= cutoff_dt:
            eligible_ids.append(row_id)

    total_eligible = len(eligible_ids)
    total_slots = 0
    total_tests = 0
    total_requests = 0
    batches_count = 0

    # Process in batches for count aggregation
    for i in range(0, total_eligible, batch_size):
        batch_ids = eligible_ids[i : i + batch_size]
        s_count, t_count, r_count = count_associated_records(conn, batch_ids, backend)
        total_slots += s_count
        total_tests += t_count
        total_requests += r_count
        batches_count += 1

    return {
        "status": "dry_run",
        "dry_run": True,
        "eligible_workspaces": total_eligible,
        "slots_to_delete": total_slots,
        "tests_to_delete": total_tests,
        "requests_to_unlink": total_requests,
        "batches_calculated": batches_count,
    }


def execute_cleanup_batch(
    conn: Any,
    workspace_ids: List[int],
    backend: str,
) -> Tuple[int, int, int]:
    """Execute a single atomic cleanup batch.
    Order of operations:
    1. Count child records.
    2. Explicitly unlink request history (requests.workspace_id = NULL).
    3. Delete child model_test_runs.
    4. Delete child model_slots (purges encrypted BYOK ciphertext without reading).
    5. Delete workspace rows.
    6. Commit transaction.
    """
    if not workspace_ids:
        return 0, 0, 0

    placeholder = "%s" if backend == "postgres" else "?"
    in_clause = _make_in_clause(workspace_ids, placeholder)
    params = tuple(workspace_ids)

    cur = conn.cursor()

    # Step 1: Count child records for verification and logging
    slots_count, tests_count, reqs_count = count_associated_records(
        conn, workspace_ids, backend
    )

    # Step 2: Unlink request history
    sql_unlink_reqs = f"UPDATE requests SET workspace_id = NULL WHERE workspace_id IN ({in_clause})"
    cur.execute(sql_unlink_reqs, params)

    # Step 3: Delete model_test_runs
    sql_del_tests = f"DELETE FROM model_test_runs WHERE workspace_id IN ({in_clause})"
    cur.execute(sql_del_tests, params)

    # Step 4: Delete model_slots
    sql_del_slots = f"DELETE FROM model_slots WHERE workspace_id IN ({in_clause})"
    cur.execute(sql_del_slots, params)

    # Step 5: Delete workspaces
    sql_del_workspaces = f"DELETE FROM workspaces WHERE id IN ({in_clause})"
    cur.execute(sql_del_workspaces, params)

    return slots_count, tests_count, reqs_count


def run_cleanup(
    grace_days: int = 7,
    batch_size: int = 100,
    dry_run: bool = False,
    db_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Main execution routine for workspace cleanup."""
    if db_url:
        os.environ["DATABASE_URL"] = db_url
        db.close_pg_pool()
    elif os.environ.get("CLEANUP_DATABASE_URL", "").strip():
        os.environ["DATABASE_URL"] = os.environ["CLEANUP_DATABASE_URL"].strip()
        db.close_pg_pool()

    start_time = time.monotonic()
    backend = db.get_backend()
    cutoff_dt, cutoff_iso = compute_cutoff(grace_days)

    target_display = "SQLite"
    if backend == "postgres":
        raw_url = db.get_database_url()
        target_display = db.mask_database_url(raw_url) if raw_url else "PostgreSQL"

    summary: Dict[str, Any] = {
        "status": "pending",
        "dry_run": dry_run,
        "backend": backend,
        "database_target": target_display,
        "grace_days": grace_days,
        "cutoff": cutoff_iso,
        "batches_completed": 0,
        "workspaces_deleted": 0,
        "slots_deleted": 0,
        "tests_deleted": 0,
        "requests_unlinked": 0,
        "duration_ms": 0.0,
        "error": None,
    }

    # PostgreSQL Session Advisory Lock handling
    advisory_conn = None
    lock_acquired = False

    try:
        if backend == "postgres":
            pool = db.get_pg_pool()
            advisory_conn = pool.getconn()
            advisory_conn.autocommit = True
            with advisory_conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_try_advisory_lock(%s) AS acquired;",
                    (CLEANUP_ADVISORY_LOCK_ID,),
                )
                res = cur.fetchone()
                lock_acquired = bool(res["acquired"] if hasattr(res, "keys") else res[0])

            if not lock_acquired:
                summary["status"] = "locked"
                summary["message"] = (
                    "PostgreSQL advisory lock already held by another cleanup process. Exiting safely."
                )
                summary["duration_ms"] = round((time.monotonic() - start_time) * 1000, 2)
                return summary

        # Open working database connection
        with db.get_connection() as conn:
            if backend == "sqlite":
                # Ensure bounded busy timeout on SQLite connection
                conn.execute("PRAGMA busy_timeout = 5000;")

            # Dry-run execution
            if dry_run:
                dry_res = run_dry_run(conn, backend, cutoff_dt, cutoff_iso, batch_size)
                summary.update(dry_res)
                summary["status"] = "dry_run"
                summary["duration_ms"] = round((time.monotonic() - start_time) * 1000, 2)
                return summary

            # Active cleanup loop using per-batch transactions
            while True:
                # Query next batch of candidate workspace IDs
                cur = conn.cursor()
                if backend == "postgres":
                    sql_select_batch = (
                        "SELECT id, expires_at FROM workspaces WHERE expires_at <= %s "
                        "ORDER BY id ASC LIMIT %s FOR UPDATE SKIP LOCKED"
                    )
                    cur.execute(sql_select_batch, (cutoff_iso, batch_size))
                else:
                    sql_select_batch = (
                        "SELECT id, expires_at FROM workspaces "
                        "WHERE strftime('%Y-%m-%d %H:%M:%f', expires_at) <= strftime('%Y-%m-%d %H:%M:%f', ?) "
                        "ORDER BY id ASC LIMIT ?"
                    )
                    cur.execute(sql_select_batch, (cutoff_iso, batch_size))

                batch_rows = cur.fetchall()

                if not batch_rows:
                    break

                batch_ids = []
                for row in batch_rows:
                    row_id = row["id"] if hasattr(row, "keys") else row[0]
                    exp_val = row["expires_at"] if hasattr(row, "keys") else row[1]
                    dt = parse_iso_utc(exp_val, ws_id=row_id)
                    if dt <= cutoff_dt:
                        batch_ids.append(row_id)

                if not batch_ids:
                    # No candidates in this slice satisfied exact microsecond cutoff
                    break

                # Process batch inside an explicit transaction
                try:
                    if backend == "sqlite":
                        conn.execute("BEGIN IMMEDIATE;")
                    
                    s_count, t_count, r_count = execute_cleanup_batch(
                        conn, batch_ids, backend
                    )
                    conn.commit()

                    summary["batches_completed"] += 1
                    summary["workspaces_deleted"] += len(batch_ids)
                    summary["slots_deleted"] += s_count
                    summary["tests_deleted"] += t_count
                    summary["requests_unlinked"] += r_count

                except Exception as batch_exc:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    summary["status"] = (
                        "partial_failure"
                        if summary["batches_completed"] > 0
                        else "failed"
                    )
                    summary["error"] = f"Batch failed: {batch_exc}"
                    raise CleanupError(summary["error"]) from batch_exc

            summary["status"] = "success"

    except CleanupError as ce:
        summary["status"] = (
            "partial_failure" if summary["batches_completed"] > 0 else "failed"
        )
        if not summary.get("error"):
            summary["error"] = str(ce)
    except Exception as exc:
        summary["status"] = (
            "partial_failure" if summary["batches_completed"] > 0 else "failed"
        )
        summary["error"] = str(exc)
    finally:
        # Guarantee PostgreSQL advisory lock release
        if advisory_conn is not None and lock_acquired:
            try:
                with advisory_conn.cursor() as cur:
                    cur.execute(
                        "SELECT pg_advisory_unlock(%s);",
                        (CLEANUP_ADVISORY_LOCK_ID,),
                    )
            except Exception:
                pass
            finally:
                try:
                    pool = db.get_pg_pool()
                    pool.putconn(advisory_conn)
                except Exception:
                    try:
                        advisory_conn.close()
                    except Exception:
                        pass

        summary["duration_ms"] = round((time.monotonic() - start_time) * 1000, 2)

    return summary


def format_text_summary(summary: Dict[str, Any]) -> str:
    """Format execution summary as clean human-readable text."""
    lines = [
        "=" * 50,
        "NEUROROUTE WORKSPACE CLEANUP SUMMARY",
        "=" * 50,
        f"Status:             {summary.get('status', 'unknown').upper()}",
        f"Dry Run:            {summary.get('dry_run')}",
        f"Backend:            {summary.get('backend')}",
        f"Database Target:    {summary.get('database_target')}",
        f"Grace Period:       {summary.get('grace_days')} days",
        f"Cutoff (UTC):       {summary.get('cutoff')}",
    ]

    if summary.get("dry_run"):
        lines.extend([
            f"Eligible Workspaces:{summary.get('eligible_workspaces', 0)}",
            f"Slots to Delete:    {summary.get('slots_to_delete', 0)}",
            f"Tests to Delete:    {summary.get('tests_to_delete', 0)}",
            f"Requests to Unlink: {summary.get('requests_to_unlink', 0)}",
            f"Batches Calculated: {summary.get('batches_calculated', 0)}",
        ])
    else:
        lines.extend([
            f"Batches Completed:  {summary.get('batches_completed', 0)}",
            f"Workspaces Deleted: {summary.get('workspaces_deleted', 0)}",
            f"Slots Deleted:      {summary.get('slots_deleted', 0)}",
            f"Tests Deleted:      {summary.get('tests_deleted', 0)}",
            f"Requests Unlinked:  {summary.get('requests_unlinked', 0)}",
        ])

    lines.extend([
        f"Duration:           {summary.get('duration_ms', 0.0)} ms",
    ])

    if summary.get("message"):
        lines.append(f"Message:            {summary.get('message')}")
    if summary.get("error"):
        lines.append(f"Error:              {summary.get('error')}")

    lines.append("=" * 50)
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    """Entry point for the CLI utility."""
    try:
        args = parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code != 0 else 0

    summary = run_cleanup(
        grace_days=args.grace_days,
        batch_size=args.batch_size,
        dry_run=args.dry_run,
        db_url=args.db_url,
    )

    if args.format == "json":
        print(json.dumps(summary, indent=2))
    else:
        print(format_text_summary(summary))

    status = summary.get("status")
    if status in ("success", "dry_run", "locked"):
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
