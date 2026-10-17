-- =============================================================================
-- NeuroRoute PostgreSQL Least-Privilege Role Setup Script (Milestone G7.11.1)
-- =============================================================================
-- WARNING: DO NOT run this script directly against existing production or
-- shared test databases without reviewing your environment and cloud provider.
--
-- This script configures three distinct database roles with least-privilege boundaries:
--
-- 1. neuroroute_admin:
--    - Used for schema migrations (migrations/postgres/*.sql) and data migrations.
--    - Holds DDL privileges (CREATE TABLE, ALTER TABLE, DROP TABLE, CREATE INDEX).
--    - Owns schema objects and sequences; sets default privileges for future objects.
--
-- 2. neuroroute_app:
--    - Used by the normal NeuroRoute gateway at runtime.
--    - Holds DML privileges (SELECT, INSERT, UPDATE) on application tables.
--    - Holds USAGE, SELECT on identity sequences for record creation.
--    - NO DDL privileges (cannot CREATE, ALTER, or DROP tables/sequences).
--    - NO DELETE privileges on workspaces, model_test_runs, or requests.
--
-- 3. neuroroute_cleanup:
--    - Used exclusively by scripts/cleanup_expired_workspaces.py.
--    - Can SELECT and DELETE expired workspaces, child model slots, and test runs.
--    - Can SELECT and UPDATE requests.workspace_id = NULL (anonymization/unlinking).
--    - Holds SELECT and UPDATE on workspaces for "FOR UPDATE SKIP LOCKED" row locking.
--    - NO INSERT privileges on any table.
--    - NO sequence privileges.
--    - NO DDL privileges.
--
-- PASSWORDS:
-- Passwords must NEVER be committed to version control. Replace the placeholders
-- below or run with psql variables:
--   psql -v ADMIN_PW='...' -v APP_PW='...' -v CLEANUP_PW='...' -f setup_postgres_roles.sql
-- =============================================================================

\set ON_ERROR_STOP on

-- 1. Create Roles Idempotently (Without embedded variables in DO blocks)
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'neuroroute_admin') THEN
        CREATE ROLE neuroroute_admin WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
    END IF;

    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'neuroroute_app') THEN
        CREATE ROLE neuroroute_app WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
    END IF;

    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'neuroroute_cleanup') THEN
        CREATE ROLE neuroroute_cleanup WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
    END IF;
END $$;

-- Set or update role passwords using psql variables (outside DO blocks so variables expand)
ALTER ROLE neuroroute_admin WITH PASSWORD :'ADMIN_PW';
ALTER ROLE neuroroute_app WITH PASSWORD :'APP_PW';
ALTER ROLE neuroroute_cleanup WITH PASSWORD :'CLEANUP_PW';

-- 2. Database Connection Privileges
-- Uses dynamic SQL to grant CONNECT on current target database without hardcoding database name
DO $$
BEGIN
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO neuroroute_admin;', current_database());
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO neuroroute_app;', current_database());
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO neuroroute_cleanup;', current_database());
END $$;

-- 3. Schema Usage & Creation Privileges
GRANT USAGE, CREATE ON SCHEMA public TO neuroroute_admin;
GRANT USAGE ON SCHEMA public TO neuroroute_app;
GRANT USAGE ON SCHEMA public TO neuroroute_cleanup;

-- Ensure public schema does not allow arbitrary table creation by non-admin roles
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM neuroroute_app;
REVOKE CREATE ON SCHEMA public FROM neuroroute_cleanup;

-- 4. Transfer Table and Sequence Ownership to neuroroute_admin (if existing)
DO $$
DECLARE
    tbl text;
    seq text;
BEGIN
    FOR tbl IN SELECT tablename FROM pg_tables WHERE schemaname = 'public'
    LOOP
        EXECUTE format('ALTER TABLE public.%I OWNER TO neuroroute_admin;', tbl);
    END LOOP;

    FOR seq IN SELECT sequencename FROM pg_sequences WHERE schemaname = 'public'
    LOOP
        EXECUTE format('ALTER SEQUENCE public.%I OWNER TO neuroroute_admin;', seq);
    END LOOP;
END $$;

-- 5. Privileges for neuroroute_admin
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO neuroroute_admin;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO neuroroute_admin;

-- Default privileges for future objects created by neuroroute_admin
ALTER DEFAULT PRIVILEGES FOR ROLE neuroroute_admin IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO neuroroute_app;

ALTER DEFAULT PRIVILEGES FOR ROLE neuroroute_admin IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO neuroroute_app;

-- 6. Privileges for neuroroute_app (Application Runtime)
-- Conditional grants on existing tables to guarantee idempotency whether executed before or after initial migration:
DO $$
BEGIN
    -- workspaces
    IF EXISTS (SELECT FROM pg_tables WHERE schemaname = 'public' AND tablename = 'workspaces') THEN
        GRANT SELECT, INSERT, UPDATE ON TABLE public.workspaces TO neuroroute_app;
        REVOKE DELETE ON TABLE public.workspaces FROM neuroroute_app;
    END IF;

    -- model_slots
    IF EXISTS (SELECT FROM pg_tables WHERE schemaname = 'public' AND tablename = 'model_slots') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.model_slots TO neuroroute_app;
    END IF;

    -- model_test_runs
    IF EXISTS (SELECT FROM pg_tables WHERE schemaname = 'public' AND tablename = 'model_test_runs') THEN
        GRANT SELECT, INSERT ON TABLE public.model_test_runs TO neuroroute_app;
        REVOKE DELETE, UPDATE ON TABLE public.model_test_runs FROM neuroroute_app;
    END IF;

    -- requests
    IF EXISTS (SELECT FROM pg_tables WHERE schemaname = 'public' AND tablename = 'requests') THEN
        GRANT SELECT, INSERT ON TABLE public.requests TO neuroroute_app;
        REVOKE DELETE, UPDATE ON TABLE public.requests FROM neuroroute_app;
    END IF;

    -- schema_migrations
    IF EXISTS (SELECT FROM pg_tables WHERE schemaname = 'public' AND tablename = 'schema_migrations') THEN
        GRANT SELECT ON TABLE public.schema_migrations TO neuroroute_app;
        REVOKE INSERT, UPDATE, DELETE ON TABLE public.schema_migrations FROM neuroroute_app;
    END IF;
END $$;

-- Identity sequence generation (required for INSERTs on existing sequences):
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO neuroroute_app;

-- 7. Privileges for neuroroute_cleanup (Expired Workspace Cleanup)
DO $$
BEGIN
    -- workspaces: SELECT and DELETE for cleanup; UPDATE required by PostgreSQL for FOR UPDATE SKIP LOCKED
    IF EXISTS (SELECT FROM pg_tables WHERE schemaname = 'public' AND tablename = 'workspaces') THEN
        GRANT SELECT, UPDATE, DELETE ON TABLE public.workspaces TO neuroroute_cleanup;
    END IF;

    -- model_slots: SELECT and DELETE child slots
    IF EXISTS (SELECT FROM pg_tables WHERE schemaname = 'public' AND tablename = 'model_slots') THEN
        GRANT SELECT, DELETE ON TABLE public.model_slots TO neuroroute_cleanup;
    END IF;

    -- model_test_runs: SELECT and DELETE child test runs
    IF EXISTS (SELECT FROM pg_tables WHERE schemaname = 'public' AND tablename = 'model_test_runs') THEN
        GRANT SELECT, DELETE ON TABLE public.model_test_runs TO neuroroute_cleanup;
    END IF;

    -- requests: SELECT and UPDATE to unlink workspace_id (set to NULL)
    IF EXISTS (SELECT FROM pg_tables WHERE schemaname = 'public' AND tablename = 'requests') THEN
        GRANT SELECT, UPDATE (workspace_id) ON TABLE public.requests TO neuroroute_cleanup;
    END IF;

    -- schema_migrations: explicitly revoke access from cleanup role
    IF EXISTS (SELECT FROM pg_tables WHERE schemaname = 'public' AND tablename = 'schema_migrations') THEN
        REVOKE ALL ON TABLE public.schema_migrations FROM neuroroute_cleanup;
    END IF;
END $$;

-- Prohibit cleanup role from inserting or touching sequences
REVOKE INSERT ON ALL TABLES IN SCHEMA public FROM neuroroute_cleanup;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM neuroroute_cleanup;
