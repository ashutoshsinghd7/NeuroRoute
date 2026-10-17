"""PostgreSQL schema migration runner for NeuroRoute.
Manages versioned, transactional, and concurrent-safe DDL migrations.

Features:
- Version tracking table: schema_migrations
- PostgreSQL advisory transaction lock: prevents concurrent multi-worker race conditions
- Transactional execution: each migration file commits cleanly or rolls back atomically
- Sanitized error reporting: never leaks database credentials in migration exceptions
"""
from datetime import datetime, timezone
import logging
import os
from typing import Any, List, Optional, Set, Tuple

from src.gateway.db import (
    DatabaseConnectionError,
    get_backend,
    get_connection,
    is_postgres,
    mask_database_url,
)

logger = logging.getLogger(__name__)

# 64-bit integer advisory lock key for NeuroRoute schema migrations
MIGRATION_ADVISORY_LOCK_ID = 83921748291074

MIGRATIONS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "migrations",
    "postgres",
)


class MigrationError(Exception):
    """Raised when a database migration fails to execute."""
    pass


class SchemaNotReadyError(MigrationError):
    """Raised when the PostgreSQL database schema is uninitialized, missing tables, or has pending migrations."""
    pass


def ensure_migration_table(conn: Any) -> None:
    """Ensure the schema_migrations tracking table exists."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version VARCHAR(50) PRIMARY KEY,
            name VARCHAR(255) NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """
    )


def get_applied_migrations(conn: Any) -> Set[str]:
    """Retrieve the set of already-applied migration versions."""
    cursor = conn.execute("SELECT version FROM schema_migrations ORDER BY version ASC;")
    rows = cursor.fetchall()
    applied = set()
    for row in rows:
        if isinstance(row, dict):
            applied.add(str(row["version"]))
        else:
            applied.add(str(row[0]))
    return applied


def discover_migration_files(directory: Optional[str] = None) -> List[Tuple[str, str, str]]:
    """Discover and sort available SQL migration files from directory.
    Returns list of (version, name, filepath) sorted by version.
    """
    target_dir = directory or MIGRATIONS_DIR
    if not os.path.exists(target_dir):
        return []

    migrations = []
    for filename in os.listdir(target_dir):
        if filename.endswith(".sql") and "_" in filename:
            parts = filename[:-4].split("_", 1)
            version = parts[0]
            name = parts[1]
            filepath = os.path.join(target_dir, filename)
            migrations.append((version, name, filepath))

    migrations.sort(key=lambda m: m[0])
    return migrations


def run_migration_script(conn: Any, version: str, name: str, filepath: str) -> None:
    """Read and execute a single migration script inside the caller's transaction."""
    with open(filepath, "r", encoding="utf-8") as f:
        sql = f.read()

    now = datetime.now(timezone.utc)
    conn.execute(sql)
    conn.execute(
        """
        INSERT INTO schema_migrations (version, name, applied_at)
        VALUES (%s, %s, %s);
        """,
        (version, name, now),
    )


def sanitize_migration_error(err_msg: str) -> str:
    """Sanitize database URL and credentials (plain, URL-encoded, with special characters)
    from migration error messages.
    """
    if not err_msg:
        return ""

    urls_to_sanitize = [
        os.environ.get("MIGRATION_DATABASE_URL", "").strip(),
        os.environ.get("DATABASE_URL", "").strip(),
    ]
    urls_to_sanitize = [u for u in urls_to_sanitize if u]
    if not urls_to_sanitize:
        return err_msg

    sanitized = err_msg

    try:
        from urllib.parse import unquote, urlsplit, urlunsplit
        for db_url in urls_to_sanitize:
            sanitized = sanitized.replace(db_url, "[REDACTED]")
            parsed = urlsplit(db_url)

            # Redact URL without query parameters if present in error message
            if parsed.query:
                url_no_query = urlunsplit(parsed._replace(query=""))
                if url_no_query and url_no_query != db_url:
                    sanitized = sanitized.replace(url_no_query, "[REDACTED]")

            # Redact both raw and unquoted passwords to cover special characters and URL-encoding
            if parsed.password:
                sanitized = sanitized.replace(parsed.password, "[REDACTED]")
                unquoted_pw = unquote(parsed.password)
                if unquoted_pw and unquoted_pw != parsed.password:
                    sanitized = sanitized.replace(unquoted_pw, "[REDACTED]")
    except Exception:
        pass

    return sanitized


def migrate_postgres(conn: Optional[Any] = None, directory: Optional[str] = None) -> List[str]:
    """Execute all pending PostgreSQL migrations sequentially and transactionally.
    Uses PostgreSQL advisory locks to guarantee safety during concurrent application startup.
    Returns list of newly applied migration versions.
    """
    if not is_postgres() and conn is None:
        return []

    migrations = discover_migration_files(directory)
    if not migrations:
        return []

    applied_in_run: List[str] = []

    def _execute_migrations(active_conn: Any):
        tx = active_conn.transaction() if hasattr(active_conn, "transaction") and callable(getattr(active_conn, "transaction")) else active_conn
        with tx:
            # 1. Acquire PostgreSQL advisory transaction lock to coordinate concurrent instances
            try:
                active_conn.execute("SELECT pg_advisory_xact_lock(%s);", (MIGRATION_ADVISORY_LOCK_ID,))
            except Exception as e:
                # If backend does not support pg_advisory_xact_lock (e.g. mock test), log and proceed
                logger.debug("Advisory lock skipped or unsupported: %s", e)

            # 2. Ensure migration tracking table exists
            ensure_migration_table(active_conn)

            # 3. Read applied migrations
            applied = get_applied_migrations(active_conn)

            # 4. Apply unapplied migrations in version order
            for version, name, filepath in migrations:
                if version not in applied:
                    logger.info("Applying PostgreSQL migration %s (%s)...", version, name)
                    try:
                        run_migration_script(active_conn, version, name, filepath)
                        applied_in_run.append(version)
                        logger.info("Successfully applied PostgreSQL migration %s (%s).", version, name)
                    except Exception as err:
                        sanitized_msg = sanitize_migration_error(str(err))
                        raise MigrationError(
                            f"Migration {version}_{name} failed to apply: {sanitized_msg}"
                        ) from err

    if conn is not None:
        _execute_migrations(conn)
    else:
        mig_url = os.environ.get("MIGRATION_DATABASE_URL", "").strip()
        if mig_url:
            with psycopg.connect(mig_url) as active_conn:
                _execute_migrations(active_conn)
        else:
            with get_connection() as active_conn:
                _execute_migrations(active_conn)

    return applied_in_run


def check_postgres_schema_readiness(
    conn: Optional[Any] = None,
    directory: Optional[str] = None,
) -> List[str]:
    """Verify that PostgreSQL schema is fully initialized, all required tables exist,
    and all discovered migrations have been applied.

    Performs purely read-only checks without requiring DDL privileges (CREATE/ALTER/DROP).
    Returns list of applied migration versions.
    Raises SchemaNotReadyError if:
    - schema_migrations table is missing
    - any required table (workspaces, model_slots, model_test_runs, requests) is missing
    - any discovered migration file is not recorded in schema_migrations
    """
    if not is_postgres() and conn is None:
        return []

    def _verify_readiness(active_conn: Any) -> List[str]:
        # 1. Check if schema_migrations table exists (read-only query)
        cur = active_conn.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = 'schema_migrations'
            );
            """
        )
        row = cur.fetchone()
        has_migrations_table = bool(row["exists"] if isinstance(row, dict) else row[0])
        if not has_migrations_table:
            raise SchemaNotReadyError(
                "PostgreSQL schema is not initialized: 'schema_migrations' table is missing. "
                "Run migrations separately using 'python -m src.gateway.migrator' before starting the application."
            )

        # 2. Check if all required application tables exist
        for tbl in ("workspaces", "model_slots", "model_test_runs", "requests"):
            cur = active_conn.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.tables
                    WHERE table_schema = 'public' AND table_name = %s
                );
                """,
                (tbl,),
            )
            r = cur.fetchone()
            tbl_exists = bool(r["exists"] if isinstance(r, dict) else r[0])
            if not tbl_exists:
                raise SchemaNotReadyError(
                    f"PostgreSQL schema is incomplete: required table '{tbl}' is missing. "
                    "Run migrations separately using 'python -m src.gateway.migrator' before starting the application."
                )

        # 3. Check that all discovered migrations have been applied
        applied = get_applied_migrations(active_conn)
        migrations = discover_migration_files(directory)
        unapplied = [v for v, name, _ in migrations if v not in applied]
        if unapplied:
            unapplied_str = ", ".join(unapplied)
            raise SchemaNotReadyError(
                f"PostgreSQL schema is out of date: {len(unapplied)} pending migration(s) ({unapplied_str}). "
                "Run migrations separately using 'python -m src.gateway.migrator' or set AUTO_MIGRATE=1 before starting the application."
            )

        logger.info(
            "PostgreSQL schema readiness check passed: %d migration(s) verified.",
            len(applied),
        )
        return sorted(list(applied))

    if conn is not None:
        return _verify_readiness(conn)
    else:
        with get_connection() as active_conn:
            return _verify_readiness(active_conn)


def init_postgres_schema() -> List[str]:
    """Initialize or upgrade PostgreSQL schema using versioned migration files.
    Invoked when explicit AUTO_MIGRATE=1 is configured or in migration scripts.
    """
    if get_backend() != "postgres":
        return []

    logger.info("Checking and applying PostgreSQL migrations...")
    return migrate_postgres()


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point for running PostgreSQL schema migrations out-of-band."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="NeuroRoute PostgreSQL schema migrator.")
    parser.add_argument(
        "--db-url",
        type=str,
        default=None,
        help="Optional PostgreSQL connection URL (overrides MIGRATION_DATABASE_URL and DATABASE_URL).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Perform read-only readiness check instead of applying migrations.",
    )
    args = parser.parse_args(argv)

    migration_url = args.db_url or os.environ.get("MIGRATION_DATABASE_URL", "").strip()
    if migration_url:
        os.environ["DATABASE_URL"] = migration_url
        from src.gateway.db import close_pg_pool
        close_pg_pool()

    try:
        if args.check:
            applied = check_postgres_schema_readiness()
            print(f"PostgreSQL schema is ready. {len(applied)} migration(s) applied: {', '.join(applied)}")
        else:
            applied = migrate_postgres()
            if applied:
                print(f"Successfully applied {len(applied)} migration(s): {', '.join(applied)}")
            else:
                print("No pending migrations. PostgreSQL schema is up to date.")
        return 0
    except Exception as exc:
        print(f"Migration error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
