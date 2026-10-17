# NeuroRoute G7.14 — Final Pre-Deployment Review Report

**Date**: 2026-10-18  
**Repository**: `D:/neuroRoute/NeuroRoute`  
**Current Branch**: `workspace-slots`  
**Review Status**: **PASSED (Production-Ready)**  
**Safety Guarantees Maintained**:
- Existing databases (`neuroroute.db` with 45 rows, `neuroroute_test` with 42 rows) were untouched.
- No production infrastructure was deployed or provisioned.
- Zero credentials, API keys, or connection secrets were exposed.
- All working tree modifications and untracked files were preserved.
- Zero Git write operations (commit, push, branch) were performed.

---

## 1. Scope of Review

This review performs a comprehensive, read-only architectural and operational audit of NeuroRoute across 7 functional areas before production configuration and deployment:
1. **Core Routing & Failover Pipeline**
2. **Model Management & BYOK Credential Handling**
3. **Database Architecture, Roles, & Migration Engine**
4. **Application Security, CSRF, & Session Management**
5. **Frontend Dashboard & API Interface**
6. **Production Hosting Compatibility & Scalability**
7. **Automated Testing & Repository State**

---

## 2. Verified Findings by Functional Area

### Area 1: Core Routing
- **Complexity Classification**:
  - Implementation: [`src/classifier/predict.py`](file:///d:/neuroRoute/NeuroRoute/src/classifier/predict.py)
  - Uses rule-based keyword classification (optional ML scikit-learn model can be activated if needed).
  - Safety rule: AI estimate can **only raise** required capability level (`min_cap = max(req.min_capability, cx["level"])`), never lower it.
  - If raising capability eliminates all candidate models, the router falls back to the user's `min_capability` and flags `cx["low_confidence_routing"] = True` ([`src/gateway/router.py:L33-L37`](file:///d:/neuroRoute/NeuroRoute/src/gateway/router.py#L33-L37)).
- **Hard-Constraint Filtering**:
  - Implementation: [`src/gateway/filter.py`](file:///d:/neuroRoute/NeuroRoute/src/gateway/filter.py)
  - Pure Python filter evaluating models against: `enabled`, `status == "up"`, `context_limit >= need_ctx`, `capability >= min_cap`, `latency_ms <= max_latency_ms`, and `est_cost <= max_cost`.
  - Rejects on first failing constraint and returns descriptive reason string. Hard constraints (latency, cost, context) are never relaxed.
- **Ranking & Multi-Model Failover**:
  - Implementation: [`src/gateway/ranking.py`](file:///d:/neuroRoute/NeuroRoute/src/gateway/ranking.py), [`src/gateway/failover.py`](file:///d:/neuroRoute/NeuroRoute/src/gateway/failover.py)
  - Sorts valid candidates based on user preference (`cheapest`, `fastest`, `highest_quality`), breaking ties by lower failure rate.
  - Cascading failover walks down ranked candidates. Before each invocation attempt, rechecks remaining latency budget (`remaining = max_latency_ms - used`). Candidates with latency exceeding remaining budget are skipped with reason `"not enough time left"`.
- **Token & Cost Estimation**:
  - Implementation: [`src/gateway/tokens.py`](file:///d:/neuroRoute/NeuroRoute/src/gateway/tokens.py)
  - Character-based token heuristics (`len(prompt) // 4 + 1`).
  - Estimates cost per 1,000 tokens before calling; records actual live provider usage and cost when available ([`src/gateway/live_call.py:L60-L68`](file:///d:/neuroRoute/NeuroRoute/src/gateway/live_call.py#L60-L68)).

### Area 2: Model Management
- **Six-Slot Workspace Fleet**:
  - Implementation: [`src/gateway/workspace_repo.py`](file:///d:/neuroRoute/NeuroRoute/src/gateway/workspace_repo.py), [`migrations/postgres/001_initial_schema.sql:L21`](file:///d:/neuroRoute/NeuroRoute/migrations/postgres/001_initial_schema.sql#L21)
  - Bounded to exactly 6 slots per workspace (`CHECK (slot_number BETWEEN 1 AND 6)`).
  - Slot CRUD operations support adding, replacing, partially patching, enabling/disabling, and resetting workspace fleets.
  - `load_demo_models()` populates only vacant slots without overwriting configured slots.
- **Provider Allowlist & SSRF Protections**:
  - Implementation: [`src/providers/registry.py`](file:///d:/neuroRoute/NeuroRoute/src/providers/registry.py)
  - Providers restricted to fixed allowlist: `simulated`, `openai`, `groq`, `deepseek`, `openrouter`, `anthropic`, and `ollama`.
  - External base URLs are hardcoded constants (e.g. `https://api.openai.com/v1`). Users cannot supply arbitrary URLs, completely preventing Server-Side Request Forgery (SSRF).
- **BYOK Credential Protection (Write-Only)**:
  - Implementation: [`src/gateway/secrets_store.py`](file:///d:/neuroRoute/NeuroRoute/src/gateway/secrets_store.py), [`src/gateway/workspace_repo.py`](file:///d:/neuroRoute/NeuroRoute/src/gateway/workspace_repo.py)
  - Provider API keys are encrypted at rest using Fernet symmetric encryption (`model_slots.api_key_encrypted`).
  - API keys are write-only over HTTP. Serialized slot payloads ([`_row_to_slot()`](file:///d:/neuroRoute/NeuroRoute/src/gateway/workspace_repo.py#L50-L75)) expose only `has_key: bool` and `key_last4: Optional[str]`. Ciphertext and plaintext credentials are never returned to the frontend or logged.
  - In `PUT /api/workspace/slots/{slot}`, omitting or leaving `api_key` empty preserves the existing encrypted key without requiring re-entry.

### Area 3: Database and Migrations
- **Dual Backend Precedence**:
  - Implementation: [`src/gateway/db.py:L86-L101`](file:///d:/neuroRoute/NeuroRoute/src/gateway/db.py#L86-L101)
  - Uses `DATABASE_URL` for PostgreSQL; falls back to SQLite (`neuroroute.db`) only when `DATABASE_URL` is unset and environment is not production.
  - In production (`ENVIRONMENT=production` or `VERCEL=1`), a missing `DATABASE_URL` immediately raises [`DatabaseConfigError`](file:///d:/neuroRoute/NeuroRoute/src/gateway/db.py#L32), failing closed.
- **Decoupled Startup Readiness**:
  - Implementation: [`src/gateway/migrator.py:L207-L285`](file:///d:/neuroRoute/NeuroRoute/src/gateway/migrator.py#L207-L285)
  - Normal gateway startup runs [`check_postgres_schema_readiness()`](file:///d:/neuroRoute/NeuroRoute/src/gateway/migrator.py#L207) which queries `information_schema.tables` and `schema_migrations` in a strictly read-only fashion.
  - If tables are missing or pending migrations exist, startup aborts with [`SchemaNotReadyError`](file:///d:/neuroRoute/NeuroRoute/src/gateway/migrator.py#L40) without falling back to SQLite and without executing DDL.
- **Three-Tier Least-Privilege Architecture**:
  - Implementation: [`scripts/setup_postgres_roles.sql`](file:///d:/neuroRoute/NeuroRoute/scripts/setup_postgres_roles.sql), [`tests/test_postgres_least_privilege.py`](file:///d:/neuroRoute/NeuroRoute/tests/test_postgres_least_privilege.py)
  - `neuroroute_admin`: Holds DDL privileges (`CREATE`, `ALTER`, `DROP`), sequence ownership, and executes migrations out-of-band via `python -m src.gateway.migrator`.
  - `neuroroute_app`: Connects for gateway runtime operations. Holds DML (`SELECT, INSERT, UPDATE` on `workspaces`; `SELECT, INSERT, UPDATE, DELETE` on `model_slots`; `SELECT, INSERT` on `model_test_runs` and `requests`; `USAGE, SELECT` on sequences). Strictly denied `DELETE` on `workspaces`, `UPDATE/DELETE` on `requests`, and all DDL.
  - `neuroroute_cleanup`: Used exclusively by [`scripts/cleanup_expired_workspaces.py`](file:///d:/neuroRoute/NeuroRoute/scripts/cleanup_expired_workspaces.py). Holds `SELECT, UPDATE, DELETE` on `workspaces`, `SELECT, DELETE` on child slots/tests, and `UPDATE (workspace_id = NULL)` on `requests`. Strictly denied `INSERT`, sequence usage, and DDL.
- **Connection Pooling**:
  - Implementation: [`src/gateway/db.py:L131-L173`](file:///d:/neuroRoute/NeuroRoute/src/gateway/db.py#L131-L173)
  - Uses `psycopg_pool.ConnectionPool` with configurable bounds (`DB_POOL_MIN_SIZE`, `DB_POOL_MAX_SIZE`, `DB_POOL_TIMEOUT`, `DB_POOL_MAX_IDLE`, `DB_POOL_MAX_LIFETIME`).
  - Preflights connections with `check=check_connection` and startup `SELECT 1;` query.
  - Application lifespan closes the pool cleanly on shutdown ([`close_pg_pool()`](file:///d:/neuroRoute/NeuroRoute/src/gateway/db.py#L175)).

### Area 4: Security
- **Production Secret Validation**:
  - Implementation: [`src/gateway/secrets_store.py:L28-L58`](file:///d:/neuroRoute/NeuroRoute/src/gateway/secrets_store.py#L28-L58)
  - In production (`ENVIRONMENT=production` or `VERCEL=1`), verifies `NEUROROUTE_SECRET_KEY` is present and is a valid 32-byte Fernet key. Raises [`SecretConfigError`](file:///d:/neuroRoute/NeuroRoute/src/gateway/secrets_store.py#L23) with sanitized error messages if missing or malformed.
- **CSRF Defense-in-Depth**:
  - Implementation: [`src/gateway/main.py:L74-L164`](file:///d:/neuroRoute/NeuroRoute/src/gateway/main.py#L74-L164)
  - Multi-layer protection on state-changing endpoints (`POST`, `PUT`, `PATCH`, `DELETE`):
    1. Rejects `Sec-Fetch-Site: cross-site` with 403 `CSRF_FAILED`.
    2. Validates `Origin` matches `Host`.
    3. Referer fallback validation when `Origin` is omitted.
    4. Rejects body-less authenticated state-changing requests when Origin, Referer, and Fetch Metadata are all absent.
    5. Enforces `Content-Type: application/json` on all requests with a body.
    6. Untrusted proxy headers: `X-Forwarded-Host` is ignored unless `TRUST_PROXY_HEADERS=1` or `VERCEL=1` is explicitly set.
- **Session Isolation & Sliding Expiration**:
  - Implementation: [`src/gateway/session.py`](file:///d:/neuroRoute/NeuroRoute/src/gateway/session.py), [`src/gateway/workspace_repo.py`](file:///d:/neuroRoute/NeuroRoute/src/gateway/workspace_repo.py)
  - 32-byte random session token issued in `nr_session` cookie (`HttpOnly`, `SameSite=Lax`, `Path=/`, dynamic `Secure` flag).
  - Only the SHA-256 hash is persisted; raw tokens are never stored in the database.
  - Workspaces slide expiration by 30 days on legitimate activity, throttled to at most once per hour unless remaining lifetime is under 24 hours (emergency slide).
  - Expired workspaces cannot be revived (`zero rowcount update returns None`).
- **Rate Limiting**:
  - Implementation: [`src/gateway/live_call.py:L28-L54`](file:///d:/neuroRoute/NeuroRoute/src/gateway/live_call.py#L28-L54)
  - In-memory sliding window rate limiter: max 20 live calls per minute per workspace.
  - Prompt length capped at 8,000 characters (400 `PROMPT_TOO_LONG`).

### Area 5: Frontend and API
- **Web UI & Workflows**:
  - Implementation: [`src/dashboard/index.html`](file:///d:/neuroRoute/NeuroRoute/src/dashboard/index.html)
  - Single-page application served directly by FastAPI at `/` using static files mount.
  - Features: real-time slot gauges, slot configuration modal with live/simulated switching, write-only API key handling, provider dropdown, latency testing, query routing playground, decision trace inspector, rejection reasons breakdown, and request history log.
- **Browser Playwright E2E Coverage**:
  - Implementation: [`tests/browser/test_dashboard_e2e.py`](file:///d:/neuroRoute/NeuroRoute/tests/browser/test_dashboard_e2e.py)
  - 10 automated browser tests validating initial load, navigation, modal open/close, client-side validation, demo model loading, routing executions, decision trace rendering, request history display, write-only BYOK key updates, and error handling.

### Area 6: Production Compatibility
- **Serverless & Multi-Worker Constraints**:
  - Process-local rate limiter: The 20 calls/min live BYOK rate limiter is stored in memory per worker process. In horizontally autoscaled multi-pod environments or multi-worker Uvicorn setups (`--workers N`), each worker tracks quotas independently.
  - Database connection pool: Psycopg 3 connection pools should connect via an external transaction pooler (Supabase PgBouncer or AWS RDS Proxy) in serverless environments to prevent connection exhaustion.
- **Health Check Behavior**:
  - The application mounts the dashboard UI at `/` (`GET /` returns HTML).
  - Cloud container orchestrators (Kubernetes, AWS ECS, GCP Cloud Run) benefit from a dedicated lightweight `GET /health` endpoint returning `{"status": "ok"}` without transferring frontend assets.
- **External CDN Dependency**:
  - The frontend loads Google Fonts and Tailwind Play CDN (`https://cdn.tailwindcss.com`).
  - In strict enterprise environments with locked-down Content Security Policies or air-gapped networks, these external domains must be allowlisted or bundled locally.

### Area 7: Tests and Repository State
- **Full Test Suite Results**:
  ```
  python -m pytest -q
  211 passed, 2 warnings in 47.83s
  ```
  - 201 non-browser unit/integration tests: **PASSED (100%)**
  - 10 browser Playwright tests: **PASSED (100%)**
  - 0 failed, 0 skipped.
  - 2 deprecation warnings from upstream `websockets` dependency in uvicorn (benign).
- **Git Diff & Whitespace Verification**:
  - `git diff --check`: **0 errors** (exit code 0).
  - All existing modifications in tracked files preserved; zero uncommitted deletions.
- **Database Immutability**:
  - SQLite `neuroroute.db`: 12 workspaces, 24 model_slots, 2 model_test_runs, 7 requests (45 rows total, 100% intact).
  - PostgreSQL `neuroroute_test`: 9 workspaces, 24 model_slots, 2 model_test_runs, 7 requests (42 rows total + 1 migration, 100% intact).

---

## 3. Findings Categorized by Priority

| Level | Finding | Description & Impact | Recommendation |
| :--- | :--- | :--- | :--- |
| **CRITICAL** | None | No security vulnerabilities, credential leaks, or data loss risks identified. | Proceed with production deployment planning. |
| **HIGH** | None | No architectural or functional defects blocking production deployment. | Proceed with deployment. |
| **MEDIUM** | Process-Local Rate Limiter in Multi-Pod Clusters | The 20 live calls/min BYOK rate limiter in [`src/gateway/live_call.py`](file:///d:/neuroRoute/NeuroRoute/src/gateway/live_call.py#L28) is maintained in-process memory. In multi-pod or multi-worker setups, each process enforces quotas independently. | For single-pod deployments, current behavior is fully sufficient. If scaling horizontally across multiple pods, back rate limiting with a shared Redis store. |
| **MEDIUM** | Serverless Connection Pool Limits | Direct connection pooling in serverless environments (AWS Lambda, Vercel) can exhaust PostgreSQL connections during concurrency spikes. | Connect through a transaction pooler (Supabase PgBouncer port 6543 or AWS RDS Proxy). |
| **LOW** | Missing Dedicated `/health` Probe Endpoint | Container orchestrators pinging `/` receive the full HTML dashboard rather than a lightweight JSON ping. | Add a lightweight `GET /health` endpoint returning `{"status": "ok"}` in a future maintenance release. |
| **LOW** | Frontend CDN Dependency | Dashboard HTML loads Tailwind CSS and Google Fonts via external CDN (`cdn.tailwindcss.com`). | Allowlist CDN domains in CSP headers, or bundle static CSS assets locally if deploying to air-gapped VPCs. |

---

## 4. Confirmed Deployment Blockers

**Confirmed Blockers**: **NONE**.  
NeuroRoute satisfies all core architectural, security, database least-privilege, and testing requirements.

---

## 5. Distinction Between Verified Readiness and Operational Assumptions

### Verified Local Readiness (Proven by Tests & Code Inspection)
- 100% pass rate across all 211 tests (including 10 browser E2E tests).
- Read-only schema readiness check fails fast with `SchemaNotReadyError` on unmigrated databases without falling back to SQLite or attempting DDL.
- Three-role least-privilege permissions verified via `psql` against disposable databases.
- Write-only BYOK encryption and zero credential leaks verified.
- CSRF defense-in-depth, reverse-proxy header safeguards, and sliding workspace session expiration verified.

### Operational Assumptions (Must Be Validated in Target Production Environment)
- **Database Connectivity**: Assumes managed PostgreSQL instance supports TLS (`sslmode=require`) and allows custom role creation via `setup_postgres_roles.sql`.
- **Environment Injection**: Assumes deployment platform securely injects `DATABASE_URL`, `NEUROROUTE_SECRET_KEY`, and `ENVIRONMENT=production` into process environment without exposing them in build logs.
- **Key Consistency**: Assumes the production `NEUROROUTE_SECRET_KEY` matches the Fernet key used during historical SQLite data migration.
- **Reverse Proxy**: Assumes reverse proxy properly forwards `Host` header and sets `X-Forwarded-Proto: https` (with `TRUST_PROXY_HEADERS=1` configured if reverse-proxy host rewrite is required).

---

## 6. Recommended Next Steps for Production Rollout

1. **Target Infrastructure Provisioning**:
   - Provision managed PostgreSQL database (e.g. Supabase, Neon, AWS RDS, GCP Cloud SQL) with TLS required.
2. **Execute Role Setup**:
   - Run [`scripts/setup_postgres_roles.sql`](file:///d:/neuroRoute/NeuroRoute/scripts/setup_postgres_roles.sql) via `psql` using generated high-entropy passwords for `neuroroute_admin`, `neuroroute_app`, and `neuroroute_cleanup`.
3. **Execute Out-of-Band Schema Migration**:
   - Apply migrations using `python -m src.gateway.migrator --db-url "$MIGRATION_DATABASE_URL"`.
4. **Historical Data Transfer (If Migrating Existing Workspaces)**:
   - Run `python scripts/migrate_sqlite_to_postgres.py --source-db neuroroute.db --target-url "$MIGRATION_DATABASE_URL" --dry-run`.
   - Run data transfer and verify parity via `--verify-only`.
5. **Set Production Environment Variables**:
   - Configure `ENVIRONMENT=production`.
   - Set `DATABASE_URL` pointing strictly to `neuroroute_app`.
   - Set `NEUROROUTE_SECRET_KEY` to the active 32-byte Fernet key.
6. **Launch Application Gateway**:
   - Start gateway: `python -m uvicorn src.gateway.main:app --host 0.0.0.0 --port 8000`.
   - Lifespan startup will validate secret key and verify read-only schema readiness.
