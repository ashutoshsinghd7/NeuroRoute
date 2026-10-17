# NeuroRoute G7.13 — Production Configuration Readiness Audit Report

**Date**: 2026-10-18  
**Repository**: `D:/neuroRoute/NeuroRoute`  
**Current Branch**: `workspace-slots`  
**Audit Scope**: Read-only production configuration audit, environment validation, database safety architecture, hosting compatibility, and regression verification.  
**Safety Guarantees Maintained**:
- Existing databases (`neuroroute.db` and `neuroroute_test`) were **not connected to, altered, or migrated**.
- Zero database roles or permissions were modified.
- No secrets or credentials were logged or exposed.
- All existing tracked modifications and untracked files were preserved.
- Zero Git commits, pushes, or branch switches were executed.

---

## 1. Local Readiness vs. Actual Production Readiness

| Dimension | Local State (Verified) | Production Requirements (To Be Deployed) |
| :--- | :--- | :--- |
| **Database Engine** | SQLite (`neuroroute.db`, 45 rows) & local PostgreSQL (`neuroroute_test`, 42 rows) | Managed PostgreSQL (Supabase / Neon / AWS RDS / GCP Cloud SQL) with TLS (`sslmode=require`) |
| **Role Separation** | 3-tier role architecture validated via disposable databases (`setup_postgres_roles.sql`) | Roles (`neuroroute_admin`, `neuroroute_app`, `neuroroute_cleanup`) provisioned with high-entropy passwords in production cluster |
| **Startup Mode** | Read-only schema readiness check active (`check_postgres_schema_readiness()`) | Same: `AUTO_MIGRATE=0` (or unset), preventing runtime gateway from executing DDL |
| **Secret Management** | Local developer mode passes without `NEUROROUTE_SECRET_KEY` | `ENVIRONMENT=production` enforces mandatory, valid 32-byte Fernet key at startup |
| **Network & Hosting** | Direct `localhost:8000` Uvicorn runtime | Reverse proxy (Cloudflare / Nginx / Vercel) with SSL termination and `TRUST_PROXY_HEADERS=1` |
| **Live BYOK Rate Limiting**| In-memory sliding window (20 calls/min per worker) | Single worker or external Redis store if horizontal container autoscaling is enabled |

---

## 2. Environment Configuration Audit

### A. Environment Variables Inventory

| Variable | Scope | Status | Purpose & Fail-Closed Behavior |
| :--- | :--- | :--- | :--- |
| `ENVIRONMENT` | Gateway | Optional (Mandatory in prod) | When set to `'production'` (or `ENV`, `NODE_ENV`, or `VERCEL=1`), activates strict production validations. |
| `DATABASE_URL` | Runtime | Mandatory in prod | PostgreSQL connection string for gateway runtime. **Fails closed** with `DatabaseConfigError` if missing in production. Must connect strictly as `neuroroute_app`. |
| `NEUROROUTE_SECRET_KEY` | BYOK | Mandatory in prod | 32-byte URL-safe base64 Fernet key for encrypting provider API keys at rest. **Fails closed** with `SecretConfigError` if missing or malformed in production. |
| `MIGRATION_DATABASE_URL` | Migrator | Optional | Dedicated admin connection string with DDL privileges. Used by `src/gateway/migrator.py` for out-of-band schema migrations. Connects as `neuroroute_admin`. |
| `CLEANUP_DATABASE_URL` | Maintenance | Optional | Dedicated maintenance connection string. Used by `scripts/cleanup_expired_workspaces.py` for workspace expiration purges. Connects as `neuroroute_cleanup`. |
| `AUTO_MIGRATE` | Gateway | Optional | If `'1'`, applies migrations during gateway startup. **Must remain unset or `0` in production** to preserve least-privilege guarantees. |
| `TRUST_PROXY_HEADERS` | Gateway | Optional | If `'1'`, trusts `X-Forwarded-Host` for CSRF validation. Default `False` prevents header spoofing. Enable only behind verified reverse proxies. |
| `DB_POOL_MIN_SIZE` | Database | Optional (Default `1`) | Minimum idle connections in Psycopg 3 connection pool. |
| `DB_POOL_MAX_SIZE` | Database | Optional (Default `10`) | Maximum active connections in Psycopg 3 connection pool. |
| `DB_POOL_TIMEOUT` | Database | Optional (Default `10.0`s) | Connection checkout timeout before raising error. |
| `DB_POOL_MAX_IDLE` | Database | Optional (Default `300.0`s) | Idle connection eviction timeout. |
| `DB_POOL_MAX_LIFETIME` | Database | Optional (Default `3600.0`s) | Maximum connection recycle age. |
| `NEUROROUTE_DB` | SQLite | Optional (Default `neuroroute.db`) | SQLite file path for local development. Ignored when `DATABASE_URL` is set. |
| `NEUROROUTE_SEED` | Router | Optional (Default `42`) | Integer seed for deterministic simulated model routing. |

### B. Startup Configuration & Production Detection
- `is_production_environment()` in [`src/gateway/db.py`](file:///d:/neuroRoute/NeuroRoute/src/gateway/db.py#L70) detects production via:
  `os.environ.get("VERCEL") or os.environ.get("ENVIRONMENT") == "production" or os.environ.get("ENV") == "production" or os.environ.get("NODE_ENV") == "production"`.
- If production is detected:
  1. `get_backend()` immediately raises [`DatabaseConfigError`](file:///d:/neuroRoute/NeuroRoute/src/gateway/db.py#L32) if `DATABASE_URL` is unset or empty, prohibiting ephemeral SQLite storage.
  2. `validate_production_secret()` in [`src/gateway/secrets_store.py`](file:///d:/neuroRoute/NeuroRoute/src/gateway/secrets_store.py#L28) immediately raises [`SecretConfigError`](file:///d:/neuroRoute/NeuroRoute/src/gateway/secrets_store.py#L23) if `NEUROROUTE_SECRET_KEY` is missing, empty, or not a valid 32-byte Fernet key.
- **Credential Leak Prevention**:
  - `mask_database_url()` in [`src/gateway/db.py`](file:///d:/neuroRoute/NeuroRoute/src/gateway/db.py#L53) masks passwords (`user:***@host:port/db`) in all logs and connection errors.
  - `sanitize_migration_error()` in [`src/gateway/migrator.py`](file:///d:/neuroRoute/NeuroRoute/src/gateway/migrator.py#L108) replaces full URLs, raw passwords, and URL-encoded passwords with `[REDACTED]`.
  - Exception handler in [`src/gateway/main.py`](file:///d:/neuroRoute/NeuroRoute/src/gateway/main.py#L60) replaces input values with `[REDACTED]` whenever validation errors reference `api_key`.

---

## 3. Database Configuration & Role Architecture

### A. Dedicated URL Separation
1. **`DATABASE_URL`**: Used by [`src/gateway/db.py`](file:///d:/neuroRoute/NeuroRoute/src/gateway/db.py) for the normal gateway process. Configured with least-privilege credentials:
   `postgresql://neuroroute_app:<APP_PW>@<HOST>:<PORT>/<DB>?sslmode=require`
2. **`MIGRATION_DATABASE_URL`**: Used by [`src/gateway/migrator.py`](file:///d:/neuroRoute/NeuroRoute/src/gateway/migrator.py) for out-of-band migrations and data import scripts. Configured with migration admin credentials:
   `postgresql://neuroroute_admin:<ADMIN_PW>@<HOST>:<PORT>/<DB>?sslmode=require`
3. **`CLEANUP_DATABASE_URL`**: Used by [`scripts/cleanup_expired_workspaces.py`](file:///d:/neuroRoute/NeuroRoute/scripts/cleanup_expired_workspaces.py) for batch purging expired workspaces. Configured with maintenance credentials:
   `postgresql://neuroroute_cleanup:<CLEANUP_PW>@<HOST>:<PORT>/<DB>?sslmode=require`

### B. Startup Readiness Verification
- When `AUTO_MIGRATE` is unset (default in production), the application executes [`check_postgres_schema_readiness()`](file:///d:/neuroRoute/NeuroRoute/src/gateway/migrator.py#L207).
- It executes purely read-only queries against `information_schema.tables` and `schema_migrations`.
- If any required table (`workspaces`, `model_slots`, `model_test_runs`, `requests`) is missing or any discovered migration is unapplied, startup fails immediately with [`SchemaNotReadyError`](file:///d:/neuroRoute/NeuroRoute/src/gateway/migrator.py#L40).
- **Fail-Closed Guarantee**: The gateway never silently falls back to SQLite and never attempts unauthorized DDL.

### C. Role Capability & Prohibition Matrix

| Object / Operation | `neuroroute_admin` | `neuroroute_app` | `neuroroute_cleanup` |
| :--- | :--- | :--- | :--- |
| `SCHEMA public` | `USAGE, CREATE` | `USAGE` (No `CREATE`) | `USAGE` (No `CREATE`) |
| `workspaces` | `ALL PRIVILEGES` (Owner) | `SELECT, INSERT, UPDATE` | `SELECT, UPDATE, DELETE` |
| `model_slots` | `ALL PRIVILEGES` (Owner) | `SELECT, INSERT, UPDATE, DELETE` | `SELECT, DELETE` |
| `model_test_runs` | `ALL PRIVILEGES` (Owner) | `SELECT, INSERT` | `SELECT, DELETE` |
| `requests` | `ALL PRIVILEGES` (Owner) | `SELECT, INSERT` | `SELECT, UPDATE (workspace_id)` |
| `schema_migrations`| `ALL PRIVILEGES` (Owner) | `SELECT` | `NO ACCESS` |
| Sequences (`*_id_seq`) | `ALL PRIVILEGES` (Owner) | `USAGE, SELECT` | `NO ACCESS` |
| Advisory Locks | `pg_advisory_xact_lock` | None required | `pg_try_advisory_lock` / `unlock` |
| DDL Privileges | `CREATE / ALTER / DROP` | **DENIED** (42501) | **DENIED** (42501) |

---

## 4. Migration, Recovery, and Key Continuity

### A. Safe Production Provisioning Sequence
1. **Target Database Creation**:
   Provision a dedicated PostgreSQL database on managed infrastructure (e.g. Supabase, Neon, AWS RDS, GCP Cloud SQL) enforcing TLS (`sslmode=require`).
2. **Role Provisioning**:
   Execute [`scripts/setup_postgres_roles.sql`](file:///d:/neuroRoute/NeuroRoute/scripts/setup_postgres_roles.sql) using `psql` as cluster superuser:
   ```bash
   psql -h <HOST> -p 5432 -U postgres -d <DB_NAME> \
     -v ADMIN_PW='<STRONG_ADMIN_PASSWORD>' \
     -v APP_PW='<STRONG_APP_PASSWORD>' \
     -v CLEANUP_PW='<STRONG_CLEANUP_PASSWORD>' \
     -f scripts/setup_postgres_roles.sql
   ```
3. **Out-of-Band Schema Migration**:
   Run schema migrations using `MIGRATION_DATABASE_URL` (under `neuroroute_admin`):
   ```bash
   python -m src.gateway.migrator --db-url "postgresql://neuroroute_admin:<ADMIN_PW>@<HOST>:5432/<DB>?sslmode=require"
   ```
4. **Data Import & Parity Verification (Cutover)**:
   If migrating existing SQLite data:
   ```bash
   # Preflight dry-run validation (writes zero rows)
   python scripts/migrate_sqlite_to_postgres.py --source-db neuroroute.db --target-url "$MIGRATION_DATABASE_URL" --dry-run

   # Atomic data transfer (preserves IDs and ciphertext)
   python scripts/migrate_sqlite_to_postgres.py --source-db neuroroute.db --target-url "$MIGRATION_DATABASE_URL"

   # 100% parity verification audit
   python scripts/migrate_sqlite_to_postgres.py --source-db neuroroute.db --target-url "$MIGRATION_DATABASE_URL" --verify-only
   ```
5. **Preflight Readiness Check**:
   Validate read-only runtime readiness under `neuroroute_app`:
   ```bash
   python -m src.gateway.migrator --check --db-url "$DATABASE_URL"
   ```
6. **Application Start**:
   Start application gateway with `DATABASE_URL` pointing strictly to `neuroroute_app` and `ENVIRONMENT=production`.

### B. Backup & Rollback Prerequisites
- **Snapshot Backup**: A Point-in-Time-Recovery (PITR) snapshot or pg_dump backup must be captured immediately before any schema migration or data import.
- **Transactional Rollback**: Each migration file in `migrations/postgres/*.sql` executes inside a single transaction. If a statement fails, the entire transaction rolls back automatically, leaving `schema_migrations` in its prior consistent state.
- **Failed Migration Recovery (No Blind Drops)**:
  - If a migration fails, the error message is logged with credentials redacted by `sanitize_migration_error`.
  - Inspect `schema_migrations` (`SELECT version, applied_at FROM schema_migrations;`) to verify which migrations were already applied.
  - Identify and fix the SQL defect in the failed migration file.
  - Re-run `python -m src.gateway.migrator`.
  - **NEVER** drop the database or public schema to recover from a migration failure.

### C. Fernet Key Continuity
- The `NEUROROUTE_SECRET_KEY` encrypts provider credentials into `model_slots.api_key_encrypted`.
- **Do NOT rotate or regenerate this key during deployment**. The production environment must receive the exact Fernet key used to encrypt the migrated SQLite credentials; otherwise, decrypting provider keys for live routing will fail (`InvalidToken`).

---

## 5. Hosting & Runtime Compatibility

1. **Application Lifespan & Connection Pooling**:
   - `lifespan()` in [`src/gateway/main.py`](file:///d:/neuroRoute/NeuroRoute/src/gateway/main.py#L40) validates `validate_production_secret()` and checks schema readiness on startup, and cleanly closes the pool via `close_pg_pool()` on shutdown.
   - For serverless deployments (AWS Lambda, Vercel Serverless Functions) where processes spin down frequently, direct connection pooling may exhaust database connection limits. Serverless deployments must connect via an external transaction pooler (e.g. Supabase PgBouncer on port 6543 or AWS RDS Proxy).
2. **Health Check Probes**:
   - Currently, the application mounts the UI dashboard at `/` (`app.mount("/", StaticFiles(...))`).
   - Standard cloud container orchestrators (Kubernetes, AWS ECS, GCP Cloud Run) typically ping a lightweight endpoint. While `GET /` returns HTTP 200, it transfers the frontend HTML.
   - *Recommendation*: Consider adding a dedicated, lightweight `GET /health` endpoint returning `{"status": "ok"}` for container health probes without UI overhead.
3. **Static Dashboard & Content Security Policy (CSP)**:
   - The dashboard in [`src/dashboard/index.html`](file:///d:/neuroRoute/NeuroRoute/src/dashboard/index.html) loads Tailwind CSS and Google Fonts via CDN:
     `https://cdn.tailwindcss.com` and `https://fonts.googleapis.com`.
   - In restricted VPC or strict CSP environments, these domains must be allowlisted in `Content-Security-Policy` headers (`style-src`, `font-src`, `script-src`).
4. **Live BYOK Rate Limiting in Multi-Pod Architectures**:
   - The 20 calls/minute live BYOK rate limiter in [`src/gateway/live_call.py`](file:///d:/neuroRoute/NeuroRoute/src/gateway/live_call.py#L28) is in-memory per worker process (`WorkspaceRateLimiter`).
   - If running a single container instance with single worker, this provides accurate protection.
   - If deploying with multiple container instances (horizontal autoscaling) or multi-worker Uvicorn (`--workers 4`), each worker maintains its own window.
   - *Recommendation*: In a horizontally scaled cluster, back the rate limiter with a shared Redis store to enforce cluster-wide quotas.

---

## 6. Verification & Regression Results

1. **Complete Automated Test Suite**:
   ```
   python -m pytest -q
   ================ 211 passed, 2 warnings in 57.30s ================
   ```
   - 201 non-browser unit and integration tests: **PASSED**
   - 10 browser Playwright E2E tests: **PASSED**
   - 2 benign deprecation warnings from upstream `websockets` package in uvicorn.
2. **Git Whitespace & Syntax Check**:
   ```
   git diff --check: 0 errors (exit code 0)
   ```
3. **Database Immutability Verification**:
   - SQLite `neuroroute.db`: 12 workspaces, 24 model_slots, 2 model_test_runs, 7 requests (45 rows total, 100% untouched).
   - PostgreSQL `neuroroute_test`: 9 workspaces, 24 model_slots, 2 model_test_runs, 7 requests (42 rows total + 1 migration, 100% untouched).
   - No temporary or orphan roles/databases exist.

---

## 7. Production Blockers, Remaining Decisions, and Exact Next Steps

### Blockers:
- **Zero code or architectural blockers**. The application code, least-privilege SQL scripts, and test suite are production-ready.

### Remaining Operational Decisions (To Be Decided Before Infrastructure Provisioning):
1. **Target Cloud Provider**: Select managed PostgreSQL platform (e.g. Supabase, Neon, AWS RDS, GCP Cloud SQL).
2. **Connection Pooling Architecture**: If deploying on serverless (Vercel/Lambda), configure external transaction pooler (PgBouncer/RDS Proxy). If deploying containerized (Docker/ECS/Cloud Run), internal Psycopg 3 connection pool is ready as-is.
3. **Cluster-Wide Rate Limiting**: If scaling to >1 container instance, determine whether in-memory rate limiting is acceptable or if Redis backing should be provisioned.

### Exact Next Steps:
1. Provision target cloud PostgreSQL instance with SSL enforced.
2. Execute `scripts/setup_postgres_roles.sql` via `psql` using strong generated passwords.
3. Run `python -m src.gateway.migrator` with `MIGRATION_DATABASE_URL` to create schema.
4. If migrating historical data, run `scripts/migrate_sqlite_to_postgres.py` (`--dry-run` then execute).
5. Set `ENVIRONMENT=production`, `DATABASE_URL` (app role), and `NEUROROUTE_SECRET_KEY` in production environment.
6. Launch gateway.
