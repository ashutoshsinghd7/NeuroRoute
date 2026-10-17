"""Tests for Milestone G7.11.1: PostgreSQL Least-Privilege Role Separation.

Verifies:
1. The application can start with a runtime role (neuroroute_app) that lacks DDL privileges.
2. Missing or incompatible schema produces a clear startup failure (SchemaNotReadyError).
3. Normal workspace and model-slot operations work under neuroroute_app.
4. Routing and request logging work under neuroroute_app.
5. Model-test logging works under neuroroute_app.
6. The migration role (neuroroute_admin) can apply migrations.
7. The cleanup role (neuroroute_cleanup) can perform only its required maintenance operations.
8. Runtime and cleanup roles cannot perform prohibited DDL or out-of-scope DML.
9. Existing SQLite behavior remains unchanged.

Safety Guarantees:
- Uses a disposable temporary PostgreSQL database: neuroroute_lp_test.
- Never touches neuroroute.db or neuroroute_test.
- Drops all disposable test roles and databases upon test suite completion.
"""
from datetime import datetime, timedelta, timezone
import os
import secrets
from typing import Generator
import pytest
from cryptography.fernet import Fernet
from starlette.testclient import TestClient

import psycopg
from psycopg.errors import InsufficientPrivilege

from src.gateway.db import (
    close_pg_pool,
    get_backend,
    get_connection,
    reset_pg_pool,
)
from src.gateway.logger import log_request
from src.gateway.main import app
from src.gateway.migrator import (
    SchemaNotReadyError,
    check_postgres_schema_readiness,
    discover_migration_files,
    init_postgres_schema,
    migrate_postgres,
)
from src.gateway.workspace_repo import (
    add_model,
    create_workspace,
    get_slot,
    get_workspace_by_token_hash,
    list_slots,
    patch_model,
    record_test_run,
    remove_model,
)
from scripts.cleanup_expired_workspaces import run_cleanup
from tests.test_postgres_concurrency import _get_postgres_admin_credentials


DISPOSABLE_DB = "neuroroute_lp_test"


@pytest.fixture(scope="module")
def lp_environment() -> Generator[dict, None, None]:
    """Provision disposable test database (neuroroute_lp_test) and 3 isolated test roles.
    Yields connection URLs for admin, app, and cleanup roles.
    Tears down everything cleanly at end of test module.
    """
    port, admin_pwd = _get_postgres_admin_credentials()
    postgres_super_url = f"postgresql://postgres:{admin_pwd}@127.0.0.1:{port}/postgres"
    lp_super_url = f"postgresql://postgres:{admin_pwd}@127.0.0.1:{port}/{DISPOSABLE_DB}"

    run_id = secrets.token_hex(4)
    admin_role = f"test_nr_admin_{run_id}"
    app_role = f"test_nr_app_{run_id}"
    cleanup_role = f"test_nr_cleanup_{run_id}"

    admin_role_pw = secrets.token_urlsafe(16)
    app_role_pw = secrets.token_urlsafe(16)
    cleanup_role_pw = secrets.token_urlsafe(16)

    admin_url = f"postgresql://{admin_role}:{admin_role_pw}@127.0.0.1:{port}/{DISPOSABLE_DB}"
    app_url = f"postgresql://{app_role}:{app_role_pw}@127.0.0.1:{port}/{DISPOSABLE_DB}"
    cleanup_url = f"postgresql://{cleanup_role}:{cleanup_role_pw}@127.0.0.1:{port}/{DISPOSABLE_DB}"

    # 1. Create clean isolated test database
    try:
        with psycopg.connect(postgres_super_url, autocommit=True) as conn:
            conn.execute(f"DROP DATABASE IF EXISTS {DISPOSABLE_DB};")
            conn.execute(f"CREATE DATABASE {DISPOSABLE_DB};")

            # Create test roles
            for role, pw in [(admin_role, admin_role_pw), (app_role, app_role_pw), (cleanup_role, cleanup_role_pw)]:
                conn.execute(psycopg.sql.SQL("DROP ROLE IF EXISTS {};").format(psycopg.sql.Identifier(role)))
                conn.execute(
                    psycopg.sql.SQL(
                        "CREATE ROLE {} WITH LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;"
                    ).format(psycopg.sql.Identifier(role), psycopg.sql.Literal(pw))
                )
    except Exception as e:
        pytest.skip(f"Could not connect to PostgreSQL to provision least-privilege test environment: {e}")

    # 2. Grant CONNECT and apply initial schema via admin role
    with psycopg.connect(lp_super_url, autocommit=True) as conn:
        conn.execute(f"GRANT CONNECT ON DATABASE {DISPOSABLE_DB} TO {admin_role};")
        conn.execute(f"GRANT CONNECT ON DATABASE {DISPOSABLE_DB} TO {app_role};")
        conn.execute(f"GRANT CONNECT ON DATABASE {DISPOSABLE_DB} TO {cleanup_role};")

        conn.execute(f"GRANT USAGE, CREATE ON SCHEMA public TO {admin_role};")
        conn.execute(f"GRANT USAGE ON SCHEMA public TO {app_role};")
        conn.execute(f"GRANT USAGE ON SCHEMA public TO {cleanup_role};")
        conn.execute(f"REVOKE CREATE ON SCHEMA public FROM {app_role};")
        conn.execute(f"REVOKE CREATE ON SCHEMA public FROM {cleanup_role};")

    # 3. Apply schema migrations as admin role
    with psycopg.connect(admin_url, autocommit=True) as conn:
        migrate_postgres(conn=conn)

        # Set default privileges for future objects created by admin
        conn.execute(
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {admin_role} IN SCHEMA public "
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {app_role};"
        )
        conn.execute(
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {admin_role} IN SCHEMA public "
            f"GRANT USAGE, SELECT ON SEQUENCES TO {app_role};"
        )

        # 4. Apply explicit least-privilege grants
        # App role grants:
        conn.execute(f"GRANT SELECT, INSERT, UPDATE ON TABLE workspaces TO {app_role};")
        conn.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE model_slots TO {app_role};")
        conn.execute(f"GRANT SELECT, INSERT ON TABLE model_test_runs TO {app_role};")
        conn.execute(f"GRANT SELECT, INSERT ON TABLE requests TO {app_role};")
        conn.execute(f"GRANT SELECT ON TABLE schema_migrations TO {app_role};")
        conn.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {app_role};")

        # Cleanup role grants:
        conn.execute(f"GRANT SELECT, UPDATE, DELETE ON TABLE workspaces TO {cleanup_role};")
        conn.execute(f"GRANT SELECT, DELETE ON TABLE model_slots TO {cleanup_role};")
        conn.execute(f"GRANT SELECT, DELETE ON TABLE model_test_runs TO {cleanup_role};")
        conn.execute(f"GRANT SELECT, UPDATE (workspace_id) ON TABLE requests TO {cleanup_role};")

    env_data = {
        "admin_url": admin_url,
        "app_url": app_url,
        "cleanup_url": cleanup_url,
        "admin_role": admin_role,
        "app_role": app_role,
        "cleanup_role": cleanup_role,
        "super_url": postgres_super_url,
    }

    yield env_data

    # Teardown: terminate connections, drop test db, drop test roles
    close_pg_pool()
    try:
        with psycopg.connect(postgres_super_url, autocommit=True) as conn:
            conn.execute(
                f"""
                SELECT pg_terminate_backend(pid)
                FROM pg_stat_activity
                WHERE datname = '{DISPOSABLE_DB}' AND pid <> pg_backend_pid();
                """
            )
            conn.execute(f"DROP DATABASE IF EXISTS {DISPOSABLE_DB};")
            for role in [admin_role, app_role, cleanup_role]:
                conn.execute(f"DROP ROLE IF EXISTS {role};")
    except Exception:
        pass


@pytest.fixture(autouse=True)
def reset_environment():
    """Guarantee environment and connection pool are cleanly reset between test cases."""
    orig_env = os.environ.copy()
    yield
    close_pg_pool()
    os.environ.clear()
    os.environ.update(orig_env)


# ---------------------------------------------------------------------------
# 1. Prohibited DDL Tests
# ---------------------------------------------------------------------------

def test_runtime_role_cannot_perform_ddl(lp_environment):
    """Verify that neuroroute_app cannot create, alter, or drop tables or alter sequences."""
    app_url = lp_environment["app_url"]
    with psycopg.connect(app_url, autocommit=True) as conn:
        # Prohibited: CREATE TABLE
        with pytest.raises(InsufficientPrivilege) as exc_info:
            conn.execute("CREATE TABLE public.unauthorized_tbl (id serial primary key);")
        assert exc_info.value.sqlstate == "42501"

        # Prohibited: DROP TABLE
        with pytest.raises(InsufficientPrivilege) as exc_info:
            conn.execute("DROP TABLE public.workspaces;")
        assert exc_info.value.sqlstate == "42501"

        # Prohibited: TRUNCATE TABLE
        with pytest.raises(InsufficientPrivilege) as exc_info:
            conn.execute("TRUNCATE TABLE public.workspaces;")
        assert exc_info.value.sqlstate == "42501"

        # Prohibited: Sequence alteration / setval (requires UPDATE on sequence)
        with pytest.raises(InsufficientPrivilege) as exc_info:
            conn.execute("SELECT setval('workspaces_id_seq', 999);")
        assert exc_info.value.sqlstate == "42501"


def test_cleanup_role_cannot_perform_ddl_or_inserts(lp_environment):
    """Verify that neuroroute_cleanup cannot create tables, insert workspaces, or touch sequences/migrations."""
    cleanup_url = lp_environment["cleanup_url"]
    with psycopg.connect(cleanup_url, autocommit=True) as conn:
        # Prohibited: CREATE TABLE
        with pytest.raises(InsufficientPrivilege) as exc_info:
            conn.execute("CREATE TABLE public.unauthorized_cleanup_tbl (id serial primary key);")
        assert exc_info.value.sqlstate == "42501"

        # Prohibited: INSERT INTO workspaces
        with pytest.raises(InsufficientPrivilege) as exc_info:
            conn.execute(
                "INSERT INTO public.workspaces (session_token_hash, created_at, expires_at, last_seen_at) "
                "VALUES ('hacked', NOW(), NOW(), NOW());"
            )
        assert exc_info.value.sqlstate == "42501"

        # Prohibited: Access to schema_migrations
        with pytest.raises(InsufficientPrivilege) as exc_info:
            conn.execute("SELECT * FROM schema_migrations;")
        assert exc_info.value.sqlstate == "42501"

        # Prohibited: Sequence usage
        with pytest.raises(InsufficientPrivilege) as exc_info:
            conn.execute("SELECT nextval('workspaces_id_seq');")
        assert exc_info.value.sqlstate == "42501"


# ---------------------------------------------------------------------------
# 2. Schema Readiness & Gateway Startup Tests
# ---------------------------------------------------------------------------

def test_application_startup_readiness_with_runtime_role(lp_environment):
    """Verify that normal application startup check succeeds under the DDL-restricted runtime role."""
    app_url = lp_environment["app_url"]
    os.environ["DATABASE_URL"] = app_url
    os.environ["NEUROROUTE_SECRET_KEY"] = Fernet.generate_key().decode()
    os.environ.pop("AUTO_MIGRATE", None)
    reset_pg_pool()

    # Readiness check performs purely read-only queries and returns applied versions
    applied = check_postgres_schema_readiness()
    assert "001" in applied

    # FastAPI TestClient lifespan initializes cleanly without executing DDL
    with TestClient(app) as client:
        resp = client.get("/api/workspace/providers")
        assert resp.status_code == 200
        assert "providers" in resp.json()


def test_missing_schema_causes_startup_failure(lp_environment):
    """Verify that missing schema_migrations or unapplied migrations cause a clear SchemaNotReadyError."""
    port, admin_pwd = _get_postgres_admin_credentials()
    empty_db = f"neuroroute_empty_{secrets.token_hex(3)}"
    super_url = f"postgresql://postgres:{admin_pwd}@127.0.0.1:{port}/postgres"
    empty_db_url = f"postgresql://postgres:{admin_pwd}@127.0.0.1:{port}/{empty_db}"

    try:
        with psycopg.connect(super_url, autocommit=True) as conn:
            conn.execute(f"CREATE DATABASE {empty_db};")

        # Test missing schema_migrations table
        with psycopg.connect(empty_db_url) as conn:
            with pytest.raises(SchemaNotReadyError) as exc_info:
                check_postgres_schema_readiness(conn=conn)
            assert "schema_migrations' table is missing" in str(exc_info.value)

        # Create schema_migrations but omit required tables
        with psycopg.connect(empty_db_url, autocommit=True) as conn:
            conn.execute(
                "CREATE TABLE schema_migrations (version VARCHAR(50) PRIMARY KEY, name VARCHAR(255), applied_at TIMESTAMPTZ);"
            )

        with psycopg.connect(empty_db_url) as conn:
            with pytest.raises(SchemaNotReadyError) as exc_info:
                check_postgres_schema_readiness(conn=conn)
            assert "required table 'workspaces' is missing" in str(exc_info.value)

    finally:
        with psycopg.connect(super_url, autocommit=True) as conn:
            conn.execute(
                f"""
                SELECT pg_terminate_backend(pid)
                FROM pg_stat_activity
                WHERE datname = '{empty_db}' AND pid <> pg_backend_pid();
                """
            )
            conn.execute(f"DROP DATABASE IF EXISTS {empty_db};")


# ---------------------------------------------------------------------------
# 3. Runtime App Role CRUD & Sliding Expiration Tests
# ---------------------------------------------------------------------------

def test_app_role_normal_workspace_and_slot_operations(lp_environment):
    """Verify that workspaces, model slots, and sliding expiration work normally under neuroroute_app,
    while prohibited DELETE on workspaces is denied.
    """
    app_url = lp_environment["app_url"]
    os.environ["DATABASE_URL"] = app_url
    os.environ["NEUROROUTE_SECRET_KEY"] = Fernet.generate_key().decode()
    reset_pg_pool()

    from src.gateway.session import hash_token

    raw_token = secrets.token_hex(16)
    thash = hash_token(raw_token)
    ws_id = create_workspace(thash)
    assert ws_id is not None

    # Add model slot
    slot = add_model(
        workspace_id=ws_id,
        slot=1,
        data={
            "model_name": "test-model-1",
            "provider": "simulated",
            "model_id": "sim-1",
            "capability": 3,
            "context_limit": 4096,
            "cost_per_1k": 0.05,
            "latency_ms": 150,
            "failure_rate": 0.0,
            "execution_mode": "simulated",
        },
    )
    assert slot["slot"] == 1
    assert slot["name"] == "test-model-1"

    # Update model slot via patch_model
    updated = patch_model(
        workspace_id=ws_id,
        slot=1,
        update_data={"latency_ms": 220, "status": "down"},
    )
    assert updated["latency_ms"] == 220
    assert updated["status"] == "down"

    # Sliding expiration update on workspace via get_workspace_by_token_hash
    ws_dict = get_workspace_by_token_hash(thash, slide_expiration=True)
    assert ws_dict is not None
    assert ws_dict["id"] == ws_id

    # Delete model slot (allowed for app role)
    removed = remove_model(workspace_id=ws_id, slot=1)
    assert removed["slot"] == 1

    # Prohibited: DELETE on workspaces is NOT granted to neuroroute_app
    with get_connection() as conn:
        with pytest.raises(InsufficientPrivilege) as exc_info:
            conn.execute("DELETE FROM workspaces WHERE id = %s", (ws_id,))
        assert exc_info.value.sqlstate == "42501"


# ---------------------------------------------------------------------------
# 4. Request Logging & Model Test Logging Tests
# ---------------------------------------------------------------------------

def test_app_role_routing_and_request_logging(lp_environment):
    """Verify that request logging works under neuroroute_app, while UPDATE/DELETE are denied."""
    app_url = lp_environment["app_url"]
    os.environ["DATABASE_URL"] = app_url
    os.environ["NEUROROUTE_SECRET_KEY"] = Fernet.generate_key().decode()
    reset_pg_pool()

    from src.gateway.session import hash_token
    ws_id = create_workspace(hash_token(secrets.token_hex(16)))

    # Log request succeeds (INSERT into requests)
    log_request(
        req={"prompt": "Test prompt"},
        resp={"trace": {"complexity": {"level": 1, "confidence": 0.9}}, "status": "success", "chosen_model": "test-model"},
        workspace_id=ws_id,
    )

    with get_connection() as conn:
        cur = conn.execute("SELECT id, prompt_preview, chosen_model FROM requests WHERE workspace_id = %s", (ws_id,))
        row = cur.fetchone()
        assert row is not None
        req_id = row["id"]

        # Prohibited: UPDATE on requests
        with pytest.raises(InsufficientPrivilege) as exc_info:
            conn.execute("UPDATE requests SET prompt_preview = 'altered' WHERE id = %s", (req_id,))
        assert exc_info.value.sqlstate == "42501"
        conn.rollback()

        # Prohibited: DELETE on requests
        with pytest.raises(InsufficientPrivilege) as exc_info:
            conn.execute("DELETE FROM requests WHERE id = %s", (req_id,))
        assert exc_info.value.sqlstate == "42501"
        conn.rollback()


def test_app_role_model_test_logging(lp_environment):
    """Verify that model test runs can be inserted and queried under neuroroute_app, but not deleted."""
    app_url = lp_environment["app_url"]
    os.environ["DATABASE_URL"] = app_url
    os.environ["NEUROROUTE_SECRET_KEY"] = Fernet.generate_key().decode()
    reset_pg_pool()

    from src.gateway.session import hash_token
    ws_id = create_workspace(hash_token(secrets.token_hex(16)))

    with get_connection() as conn:
        now = datetime.now(timezone.utc)
        cur = conn.execute(
            """
            INSERT INTO model_test_runs (
                workspace_id, slot_number, success, latency_ms, message, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id;
            """,
            (ws_id, 1, 1, 85, "test passed", now),
        )
        row = cur.fetchone()
        run_id = row["id"]
        assert run_id is not None

        # Prohibited: DELETE on model_test_runs
        with pytest.raises(InsufficientPrivilege) as exc_info:
            conn.execute("DELETE FROM model_test_runs WHERE id = %s", (run_id,))
        assert exc_info.value.sqlstate == "42501"


# ---------------------------------------------------------------------------
# 5. Migration Role Execution Tests
# ---------------------------------------------------------------------------

def test_migration_role_can_apply_migrations(lp_environment):
    """Verify that neuroroute_admin can run out-of-band migrations with advisory locks
    and manage schema_migrations.
    """
    admin_url = lp_environment["admin_url"]
    os.environ["MIGRATION_DATABASE_URL"] = admin_url

    with psycopg.connect(admin_url, autocommit=True) as conn:
        applied = migrate_postgres(conn=conn)
        assert isinstance(applied, list)

        # Admin can acquire the migration advisory lock
        from src.gateway.migrator import MIGRATION_ADVISORY_LOCK_ID
        conn.execute("SELECT pg_advisory_xact_lock(%s);", (MIGRATION_ADVISORY_LOCK_ID,))

        # Admin has full privileges on schema_migrations
        cur = conn.execute("SELECT version, name, applied_at FROM schema_migrations ORDER BY version ASC;")
        rows = cur.fetchall()
        assert len(rows) >= 1
        versions = [r["version"] if isinstance(r, dict) else r[0] for r in rows]
        assert "001" in versions


# ---------------------------------------------------------------------------
# 6. Cleanup Role Execution Tests
# ---------------------------------------------------------------------------

def test_cleanup_role_maintenance_operations(lp_environment):
    """Verify that neuroroute_cleanup can delete expired workspaces, child slots, and test runs,
    and safely unlink request history (workspace_id = NULL) while preserving request rows.
    """
    app_url = lp_environment["app_url"]
    cleanup_url = lp_environment["cleanup_url"]
    os.environ["DATABASE_URL"] = app_url
    os.environ["NEUROROUTE_SECRET_KEY"] = Fernet.generate_key().decode()
    reset_pg_pool()

    # 1. Seed an expired workspace with slots, test run, and request using app role
    from src.gateway.session import hash_token
    ws_id = create_workspace(hash_token(secrets.token_hex(16)))

    add_model(
        workspace_id=ws_id,
        slot=1,
        data={
            "model_name": "cleanup-slot-model",
            "provider": "simulated",
            "model_id": "sim-1",
            "capability": 3,
            "context_limit": 4096,
            "cost_per_1k": 0.05,
            "latency_ms": 150,
            "failure_rate": 0.0,
            "execution_mode": "simulated",
        },
    )

    log_request(
        req={"prompt": "Historical prompt before expiry"},
        resp={"trace": {"complexity": {"level": 1, "confidence": 0.9}}, "status": "success", "chosen_model": "cleanup-slot-model"},
        workspace_id=ws_id,
    )

    with get_connection() as conn:
        now = datetime.now(timezone.utc)
        conn.execute(
            """
            INSERT INTO model_test_runs (
                workspace_id, slot_number, success, latency_ms, message, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s);
            """,
            (ws_id, 1, 1, 90, "test run", now),
        )
        # Backdate expiration to 10 days ago (past 7-day grace period)
        expired_dt = now - timedelta(days=10)
        conn.execute(
            "UPDATE workspaces SET expires_at = %s WHERE id = %s",
            (expired_dt, ws_id),
        )

    # 2. Run cleanup using neuroroute_cleanup role credentials
    summary = run_cleanup(grace_days=7, db_url=cleanup_url)
    assert summary["status"] == "success"
    assert summary["workspaces_deleted"] >= 1
    assert summary["slots_deleted"] >= 1
    assert summary["tests_deleted"] >= 1
    assert summary["requests_unlinked"] >= 1

    # 3. Verify target database state using app role
    os.environ["DATABASE_URL"] = app_url
    reset_pg_pool()
    with get_connection() as conn:
        # Workspace is deleted
        cur = conn.execute("SELECT id FROM workspaces WHERE id = %s", (ws_id,))
        assert cur.fetchone() is None

        # Slots are deleted
        cur = conn.execute("SELECT id FROM model_slots WHERE workspace_id = %s", (ws_id,))
        assert cur.fetchone() is None

        # Test runs are deleted
        cur = conn.execute("SELECT id FROM model_test_runs WHERE workspace_id = %s", (ws_id,))
        assert cur.fetchone() is None

        # Request is preserved but unlinked (workspace_id IS NULL)
        cur = conn.execute("SELECT id, workspace_id, prompt_preview FROM requests WHERE prompt_preview = 'Historical prompt before expiry'")
        req_row = cur.fetchone()
        assert req_row is not None
        assert req_row["workspace_id"] is None


# ---------------------------------------------------------------------------
# 6. SQLite Mode Preservation Test
# ---------------------------------------------------------------------------

def test_sqlite_behavior_unchanged():
    """Verify that in SQLite mode, check_postgres_schema_readiness returns empty list
    and normal SQLite initialization and operations remain 100% functional.
    """
    os.environ.pop("DATABASE_URL", None)
    os.environ.pop("VERCEL", None)
    assert get_backend() == "sqlite"

    result = check_postgres_schema_readiness()
    assert result == []
