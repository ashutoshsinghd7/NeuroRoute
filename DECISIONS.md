# DECISIONS.md — choices made while building (read with AGENT.md, CONTEXT.md, CHECKPOINTS.md)

## Overrides of the original guides
| # | Decision | Replaces | Why |
|---|---|---|---|
| D-01 | Dashboard is **plain JavaScript** (`src/dashboard/index.html, app.js, style.css`), served by FastAPI at `/`. | Streamlit in AGENT.md / CONTEXT.md / README | Team asked for a JS frontend. Still a live judge panel, so the JC is met. Streamlit references are superseded. |
| D-02 | Classifier is **hardcoded rules by default**. The TF-IDF + Logistic Regression version is in the same file, **commented out**. | CP3 "train then use" | Project works with zero ML install. Enable the AI in 3 steps (see `docs/models-and-stack.md`). |
| D-03 | Added `src/gateway/router.py` (the whole pipeline, no web framework). | Not in REPO_STRUCTURE.md | Keeps `main.py` thin and lets tests run without FastAPI. |
| D-04 | Judge edits (up/down, latency, failure rate) change an **in-memory copy** of the registry. `Reset fleet` reloads `registry.json`. | CP10 "write to registry.json or memory" | Safe: the file stays clean for the next demo. |
| D-05 | Simulated failures are seeded by `NEUROROUTE_SEED` + model + prompt. | CP11 reproducibility | Same input gives same outcome. |
| D-06 | "Skipped" attempts appear in the trace for valid models with no time left. | CP7 step 3 | Makes the remaining-budget rule visible. |
| D-07 | 6 extra fake models are documented only (`docs/models-and-stack.md`), not loaded. | n/a | Keeps the demo registry small. Paste the JSON to add them. |

## Checkpoint status
CP0 registry.json, CP1 schemas.py, CP2 tokens.py, CP3 classifier/, CP4 filter.py, CP5 ranking.py, CP6 simulated.py + failover.py, CP7 failover.py, CP8 router.py (`NO_VALID_MODEL`), CP9 logger.py, CP10 dashboard/, CP11 tests/ + docs/. All built.

## Honest notes
- The 18 pytest tests were run with a plain Python runner in a sandbox that had no pytest, FastAPI or internet. **The live API and the dashboard were not run here.** Run `pytest` and the server once on your machine.
- `prompts.csv` has 30 rows (6 per level). Add more (about 12 per level) before enabling the ML classifier.
- The hardcoded classifier is a keyword and length heuristic. It is a guess, so low confidence raises the level by 1.

## Milestone A: Workspace Database, Guest Sessions, and Model Slots

| # | Decision | Replaces | Why |
|---|---|---|---|
| D-08 | **Empty slot = no row** in `model_slots`. Only occupied slots exist in DB. API fills the gaps to always return 6 slots. | Pre-allocating dummy rows | Hard DB guarantees: `CHECK(slot_number BETWEEN 1 AND 6)`, `UNIQUE(workspace_id, slot_number)`, and `UNIQUE(workspace_id, model_name)`. Direct inserts of a 7th slot or duplicate names fail at the database level. |
| D-09 | **Guest sessions via `nr_session` cookie with SHA-256 token hashing.** | User authentication / logins | Zero-friction instant guest isolation. Raw token is never stored in DB or logged; only SHA-256 hash is saved. Scoped per browser session (30-day TTL). |
| D-10 | **Server-side workspace resolution only.** | Client-provided workspace headers | Resolves workspace strictly from validated session cookie. Any client-provided `workspace_id` in headers/query/body is strictly ignored. |
| D-11 | **Deterministic error schema & explicit HTTP status codes.** | Default exception handling | Standardized `{"error": {"code": "...", "message": "..."}}` envelope. 400 for invalid slot numbers (not 422), 404 for empty slot operations, 409 for slot occupied / duplicate names, 415 for non-JSON bodies, 403 for CSRF Origin mismatches. |
| D-12 | **Removed broad CORS & added CSRF guard.** | Wildcard `CORSMiddleware` | With credentials and cookies enabled, wildcard CORS is unsafe. Same-origin dashboard is protected against CSRF via Origin-Host matching and JSON content-type enforcement. |

### Limitations & Honest Notes (Milestone A)
- **Serverless SQLite Ephemerality**: In local development and VM environments, SQLite persists to disk reliably. On serverless runtimes (such as Vercel Lambdas), the file system (`/tmp`) is ephemeral and separate between concurrent instances. For production serverless deployments, an external managed database (such as Supabase PostgreSQL) should replace the SQLite layer via `workspace_repo.py`.
- **Browser-scoped Guest Sessions**: Guest sessions live in browser cookies (`nr_session`). They do not synchronize across devices or survive private/incognito resets. Account-based persistence can be built on top of the workspace abstraction in future milestones.

## Milestone B: Workspace Routing, History Isolation, and Legacy Bridge

| # | Decision | Replaces | Why |
|---|---|---|---|
| D-13 | **Dual-mode `/route` (Legacy vs Workspace).** | Global single-fleet routing | Callers without `nr_session` cookie (curl, scripts) route against a static read-only copy of `registry.json` (`trace["source"] = "legacy"`). Session callers route exclusively against their isolated workspace slots (`trace["source"] = "workspace"`). Expired/invalid cookies auto-provision a fresh workspace. |
| D-14 | **First-rule disabled model check in hard filter.** | Ad-hoc model filtering | `if not model.get("enabled", True): return "model is disabled"` added as the first filter rule. Rejection appears clearly in `trace["considered"]`, preventing disabled models from ever entering ranking or failover. |
| D-15 | **Workspace-isolated request history.** | Shared global requests table | Requests table migrated non-destructively (`ALTER TABLE requests ADD COLUMN workspace_id INTEGER`) with composite index `(workspace_id, id)`. `GET /api/history` scopes queries to `workspace_id`, returning `[]` for unauthenticated sessions. |
| D-16 | **Workspace-scoped legacy endpoint bridge.** | Mutating global `MODELS` list | `GET /api/models`, `PATCH /api/models/{name}`, and `POST /api/models/reset` operate strictly on the caller's workspace, maintaining full backwards compatibility for the existing frontend while eliminating cross-user fleet tampering. |

### Limitations & Honest Notes (Milestone B)
- **Legacy NULL-Workspace History**: Historical request records logged prior to Milestone B have `workspace_id = NULL`. These rows are preserved intact in the database for auditing and forensics, but are deliberately excluded from workspace user history views.
- **Vercel / Ephemeral Serverless**: When deployed to serverless environments with cold starts, file-backed SQLite in `/tmp` resets between isolated lambdas. For production deployment on serverless infrastructure, the repository patterns in `workspace_repo.py` and `db.py` should be configured with an external PostgreSQL backend.

## Milestone C: Model Manager Dashboard, History Audit, and UI Extensions

| # | Decision | Replaces | Why |
|---|---|---|---|
| D-17 | **Model Manager integrated directly into `src/dashboard/index.html`.** | Static 6-model hardcoded display | Allows dynamic configuration of 6 model slots (add, edit, replace, disable, test, remove) within the existing retro Tailwind UI without any external build step or dependencies. |
| D-18 | **Root `index.html` left untouched.** | Synchronizing root duplicate | FastAPI specifically serves `src/dashboard/index.html` at `/`. Root `index.html` is an archival duplicate and was intentionally left untouched to prevent breaking existing build workflows. |
| D-19 | **CSRF Content-Type check scoped strictly to requests with bodies.** | Requiring JSON on body-less requests | Body-less state-changing HTTP requests (`POST /api/models/reset`, `PATCH .../toggle`, `POST .../test`, `DELETE`) do not send Content-Type headers. The check now verifies `Content-Length > 0` before requiring `application/json`, while Origin-vs-Host checking remains enforced for all mutations. |
| D-20 | **Live Model Manager feedback & dynamic routing rejection badges.** | Silent model exclusion | Shows clear eligibility warnings ("Disabled: will not be routed", "Down: will not be routed") and dynamically displays rejection reasons from `trace.considered` (e.g. "latency 3000 > 2000") directly on slot cards after each `/route` invocation. |

### Limitations & Honest Notes (Milestone C)
- **Zero-Slot Graceful Degradation**: When a workspace has 0 models configured, Model Fleet and Live Mesh indicate 0 units with clear empty states ("No models yet. Add a model to a slot.") and the router cleanly refuses incoming requests (`NO_VALID_MODEL`).
- **Simulated Test Isolation**: Slot testing calls run simulated trials without external network access, seeded deterministically by model name and configuration.

## Milestone D: Empty Workspaces + Real Provider Connections (BYOK)

| # | Decision | Replaces | Why |
|---|---|---|---|
| D-21 | **Empty workspaces by default + on-demand demo loading.** | Auto-seeding 6 default models | Workspaces start with 0 slots so users can bring their own keys (BYOK) from scratch. `POST /api/workspace/load-demo-models` populates only empty slots without overwriting configured models. `POST /api/models/reset` empties the fleet. |
| D-22 | **Symmetric encryption at rest via Fernet (`cryptography`).** | Plaintext API keys in database | All BYOK API keys are encrypted before insertion into `model_slots.api_key_encrypted` using `NEUROROUTE_SECRET_KEY`. Keys are decrypted strictly in memory at invocation time and never written to logs, traces, or HTTP responses. |
| D-23 | **Strict provider allowlist and fixed base URLs (SSRF prevention).** | Arbitrary user-supplied endpoint URLs | Disallowing custom provider URLs prevents Server-Side Request Forgery (SSRF) into private subnets, cloud metadata endpoints (169.254.169.254), or local services. Only OpenAI, Groq, DeepSeek, OpenRouter, Anthropic, and localhost Ollama are permitted. |
| D-24 | **Complete key redaction across all layers & 422 error sanitization.** | Default FastAPI error reflection | Pydantic validation errors on requests containing `api_key` are intercepted by a custom `RequestValidationError` handler, replacing `"input"` with `"[REDACTED]"`. Model representations over the API only return `has_key` (bool) and `key_last4`. |
| D-25 | **Per-workspace safety limits (8000 chars, 20 live calls/min).** | Unconstrained live API consumption | Limits prompt size to 8000 characters (returns 400 `PROMPT_TOO_LONG`) and enforces an in-memory 20 calls/minute sliding window rate limiter per workspace (returns 429 `RATE_LIMIT_EXCEEDED`) to prevent runaway API spend or denial-of-service. |
| D-26 | **Dynamic slot health degradation on live provider failure.** | Static failure rates for real APIs | Live calls catch `ProviderError`: 401 Authentication errors immediately mark slot status as `"down"` (with status reason), while 429 Rate Limits and 5xx errors mark the slot as `"degraded"`, seamlessly triggering router failover to backup models. |
| D-27 | **Actual token usage & cost attached to `trace["usage"]`.** | Estimated heuristic cost only | Provider completion responses return exact `prompt_tokens` and `completion_tokens`. Total cost is computed from custom input/output token pricing rates and appended to `trace["usage"]`, updating `trace["cost"]`. |

### Limitations & Honest Notes (Milestone D)
- **Local Fernet Key Lifecycle**: If `NEUROROUTE_SECRET_KEY` is rotated or lost, existing encrypted keys in the database cannot be decrypted and must be re-entered by the user.
- **Serverless Ephemeral Storage**: On serverless platforms with ephemeral `/tmp` storage (e.g. Vercel), SQLite databases reset across container instances. Live API keys should not be deployed to serverless environments until persistent managed storage (e.g. Postgres) and a dedicated secret management vault are integrated.
- **Single-Node In-Memory Rate Limiting**: The 20 calls/min rate limiter is maintained in memory per worker process. In multi-process or clustered deployments, rate limiting should be backed by a distributed store (e.g., Redis).

## Milestone D-Fix: Health Invariant Corrections, Usage Standardization, and Error Alignment

| # | Decision | Replaces | Why |
|---|---|---|---|
| D-28 | **Binary slot status ('up' / 'down') only; no 'degraded'.** | Setting status to "degraded" on 429/5xx (D-26) | Database schema strictly enforces `CHECK(status IN ('up', 'down'))`. Setting "degraded" violated this constraint. Now, only provider auth errors (401/403) transition a slot to `'down'` (with `status_reason = 'invalid API key'`). Transient errors (429 rate limit, 5xx server error, timeout, network error) leave the slot status unchanged (`'up'`) so the model can be retried on subsequent requests while still triggering immediate failover for the current request. In addition, `update_slot_status` guards against non-'up'/'down' arguments to protect the DB CHECK constraint. |
| D-29 | **Exact 4-key `trace["usage"]` contract in project currency (Rupees).** | Arbitrary usage keys (`prompt_tokens`, `cost_usd`, etc.) | Standardizes `trace["usage"]` to strictly `{"input_tokens", "output_tokens", "actual_cost", "latency_ms"}`. `actual_cost` is calculated in INR (₹) using `cost_per_1k_input`/`output` when provided, or falling back to `cost_per_1k`. Legacy/foreign fields (`cost_usd`, `prompt_tokens`, `completion_tokens`, `total_tokens`) are removed. |
| D-30 | **Standardized `SECRET_KEY_MISSING` error code.** | `ENCRYPTION_NOT_CONFIGURED` | Renamed error code to `SECRET_KEY_MISSING` with message `"Live mode needs NEUROROUTE_SECRET_KEY. See README."` across all BYOK key storage endpoints. |

## Milestone E: Dashboard Support for BYOK, Empty Workspaces, and Usage Tracking

| # | Decision | Replaces | Why |
|---|---|---|---|
| D-31 | **Write-only API key handling in UI.** | Prefilling or retaining sensitive keys in client state | The API key input in the modal is strictly write-only (`<input type="password" autocomplete="off" spellcheck="false">`). It is never stored in JS variables, `localStorage`, `sessionStorage`, or `IndexedDB`, and is wiped immediately on modal close, on submit success, and on submit error. |
| D-32 | **Key preservation on edit with provider integrity check.** | Forcing re-entry of credentials on model updates | Editing an existing live slot displays `"Key saved ····LAST4 — leave blank to keep it"`. If left blank on submission, the existing encrypted key is preserved on the server. If the provider is changed, the UI marks the key as required to prevent sending a mismatched credential to a different provider. |
| D-33 | **Empty-workspace and zero-slot onboarding experience.** | Assuming always pre-populated fleets | When 0 slots are configured, clear empty states are shown across the Model Manager and Playground with prominent actions to "Load demo models (simulated)". Route requests without models cleanly display the server refusal (`NO_VALID_MODEL`). Presets with target models that are missing offer an inline action to load demo models rather than failing silently. |
| D-34 | **Dual-track usage and metrics visualization.** | Single estimated cost figure | Routing success cards display side-by-side or stacked "Estimated" vs "Actual" metrics. Live completions show exact input/output tokens, calculated cost in Rupees (₹), and measured network latency from `trace.usage`. Simulated runs omit the "Actual" block. |

### Limitations & Honest Notes (Milestone E)
- **Client-Side Key Ephemerality**: Because the UI operates in write-only mode without client persistence, users cannot retrieve or view previously saved API keys from the browser; they can only overwrite them with a new key or delete the slot.
- **Provider URL Immutability**: All live providers are bound to fixed, official HTTPS base URLs to eliminate SSRF risks. Custom base URLs (e.g. self-hosted proxies) are not supported from the dashboard.
- **Single-Origin Cookie Reliance**: All workspace mutations rely on browser-scoped cookies (`nr_session`). Navigating across different browser profiles or clearing cookies creates an isolated, fresh workspace.

## Milestone G2: PostgreSQL Driver and Connection Layer

| # | Decision | Replaces | Why |
|---|---|---|---|
| D-35 | **Dual-backend abstraction via `src/gateway/db.py` (Psycopg 3 + SQLite).** | SQLite-only connection factory | Allows seamless operation across local offline SQLite environments and PostgreSQL deployments via `DATABASE_URL` without introducing a heavyweight ORM. |
| D-36 | **Managed Psycopg 3 connection pooling with bounded sizing.** | Unpooled per-request connections | Employs `psycopg_pool.ConnectionPool` (`min_size=1`, `max_size=10`, `timeout=10.0`, `row_factory=dict_row`, `autocommit=False`) to avoid connection exhaustion in serverless and multi-threaded execution. |
| D-37 | **Deterministic database selection precedence and production enforcement.** | Implicit fallback to local SQLite | In production/Vercel environments, a missing `DATABASE_URL` immediately raises `DatabaseConfigError` rather than silently using ephemeral SQLite storage. If PostgreSQL is configured but fails to connect, `DatabaseConnectionError` is raised without falling back to SQLite. Local development and test fixtures continue using SQLite when `DATABASE_URL` is unset. |
| D-38 | **Backend-aware query dispatcher `q()` and `execute_insert_returning_id`.** | Fragile regex/string SQL rewriting | Explicitly separates SQLite (`?`, `cursor.lastrowid`) from PostgreSQL (`%s`, `RETURNING id`) queries, preventing accidental replacement of characters inside SQL string literals. |
| D-39 | **Unified integrity error handling (`DB_INTEGRITY_ERRORS`).** | SQLite-specific `sqlite3.IntegrityError` only | Catches both `sqlite3.IntegrityError` and `psycopg.IntegrityError`, preserving consistent 409 conflict error mapping across both backends. |
| D-40 | **FastAPI application lifespan with graceful pool cleanup.** | Startup-time uncontrolled connection initialization | Integrates `lifespan` context manager into `FastAPI(lifespan=lifespan)` to cleanly close the connection pool on application shutdown and restrict import-time DDL initialization strictly to SQLite mode. |

### Limitations & Honest Notes (Milestone G2)
- **Schema Creation Deferred to G3**: Milestone G2 prepares the driver, connection pooling, and query abstraction. It deliberately does not execute DDL on production PostgreSQL; production table creation belongs to Milestone G3.
- **Mocked PostgreSQL Unit Testing**: Unit tests for PostgreSQL connection pooling and transaction rollbacks utilize mocks when a live PostgreSQL instance is not available locally, ensuring fast, fully offline CI execution.

## Milestone G3: PostgreSQL Schema and DDL Scripts

| # | Decision | Replaces | Why |
|---|---|---|---|
| D-41 | **Versioned file-based migrations in `migrations/postgres/`.** | Embedded DDL strings in Python code | Separates DDL schema definitions into reviewable, numbered SQL files (`001_initial_schema.sql`) for clean versioning and auditability. |
| D-42 | **Dedicated migration tracking table (`schema_migrations`).** | Ad-hoc table existence checks | Tracks applied migration versions (`version`, `name`, `applied_at`) transactionally so already-applied migrations are never executed twice. |
| D-43 | **PostgreSQL advisory transaction locks for concurrent startup.** | Uncoordinated multi-worker startup | Uses `pg_advisory_xact_lock(83921748291074)` inside the migration transaction, guaranteeing that multiple uvicorn workers or serverless instances starting concurrently do not run duplicate migrations or collide. |
| D-44 | **Strict constraint parity with PostgreSQL native types.** | Permissive SQLite column types | Maps SQLite constraints to PostgreSQL equivalents: `BIGINT GENERATED ALWAYS AS IDENTITY`, `SMALLINT` for slots 1–6 and capabilities 1–5, explicit `UNIQUE` constraint names for error mapping, `ON DELETE CASCADE` for slot child rows, and `ON DELETE SET NULL` for legacy request history rows with `workspace_id = NULL`. |
| D-45 | **Lifespan-integrated non-import-time schema execution.** | Top-level module import DDL side-effects | Schema migration runs strictly inside FastAPI `lifespan` when `get_backend() == "postgres"`, eliminating unexpected network operations or migration side-effects when importing modules in tests or scripts. |

### Limitations & Honest Notes (Milestone G3)
- **Data Migration Deferred to G4**: Milestone G3 provides the schema creation scripts, version tracking, and initialization runner. It deliberately does not copy or migrate data from `neuroroute.db`. Existing SQLite data remains untouched until Milestone G4.
- **Environment Configuration**: PostgreSQL mode requires `DATABASE_URL` (format: `postgresql://[user]:[password]@[host]:[port]/[database]?sslmode=require`). Local development and tests continue using SQLite when `DATABASE_URL` is omitted.
- **Mocked Schema Testing**: Automated test coverage for DDL validation and migration idempotency uses mocked PostgreSQL connections when a live PostgreSQL server is not configured in the test environment. Real integration with a live database will occur during cutover.

## Milestone G4: Data Migration Script and Verification

| # | Decision | Replaces | Why |
|---|---|---|---|
| D-46 | **Standalone migration utility (`scripts/migrate_sqlite_to_postgres.py`).** | Automatic data migration on application startup | Decouples data migration from runtime request serving and uvicorn startup. Prevents accidental long-running migration locks during routine server boots. |
| D-47 | **Strict destination emptiness verification (`TargetNotEmptyError`).** | Blind upsert or destructive table clearing | Protects destination database from partial overwrites, duplicate keys, or accidental TRUNCATE. Halts immediately if destination tables contain pre-existing data unless explicitly reviewed. |
| D-48 | **Explicit ID preservation via `OVERRIDING SYSTEM VALUE`.** | Silently re-allocating new surrogate keys | Preserves original SQLite primary keys across all 4 tables (`workspaces`, `model_slots`, `model_test_runs`, `requests`), ensuring foreign key integrity and historic log continuity remain 100% identical. |
| D-49 | **Post-migration identity sequence synchronization.** | Disconnected auto-increment counters | Resets PostgreSQL identity sequences (`setval`) to `MAX(id) + 1` post-migration so subsequent application writes never trigger primary-key collisions. Safely handles empty tables. |
| D-50 | **Opaque ciphertext preservation for BYOK secrets.** | Plaintext export or re-encryption during migration | `model_slots.api_key_encrypted` is transferred strictly as ciphertext. The migration utility never decrypts, transforms, or logs credentials, ensuring compatibility with the active `NEUROROUTE_SECRET_KEY`. |
| D-51 | **100% post-migration verification with dry-run support.** | Heuristic or sample-only data validation | Compares 100% of row counts, PK ID sets, foreign keys, nullable workspace references, and slot attributes. `--dry-run` performs full preflight checks without writing data. |
| D-52 | **Non-closing transaction context managers (`conn.transaction()`).** | `with conn:` context blocks | In unpooled Psycopg 3, exiting `with conn:` calls `conn.close()`. Using `with conn.transaction():` ensures transactions commit or rollback without closing the underlying connection, permitting connection reuse across schema initialization and data migration. |

### Limitations & Honest Notes (Milestone G4)
- **Source Database Immutability**: The source SQLite database (`neuroroute.db`) remains the strict source of truth and is opened exclusively in read-only mode during migration.
- **Mocked PostgreSQL Verification**: When an external PostgreSQL instance is not provisioned in the local development environment, unit tests execute against mock connections to validate SQL queries, topological ordering, rollback semantics, and sequence synchronization logic. Live migration verification will be performed when target credentials are provided in Milestone G5.
- **Ciphertext Key Decoupling**: If `NEUROROUTE_SECRET_KEY` is rotated independently of the database migration, existing BYOK credentials will require re-entry by workspace users.

## Milestone G5: PostgreSQL Integration and Migration Verification

| # | Decision | Replaces | Why |
|---|---|---|---|
| D-53 | **Safe default for `max_output_tokens` in repository CUD methods.** | Passing unvalidated `None` to `NOT NULL DEFAULT 512` columns | PostgreSQL strictly enforces `NOT NULL` constraints on columns even when defaults exist if an explicit `NULL` value is supplied in an `INSERT`/`UPDATE` statement. Setting `max_out = data.get("max_output_tokens") if data.get("max_output_tokens") is not None else 512` satisfies PostgreSQL constraints while maintaining SQLite compatibility. |
| D-54 | **Passwordless PostgreSQL local authentication via `pgpass.conf`.** | Embedding plaintext passwords in command-line arguments or bash history | Avoids exposing target credentials in shell history, process tables (`ps`), or CI console logs while allowing standard psycopg connection strings to authenticate reliably. |

### Limitations & Honest Notes (Milestone G5)
- **Local Target Verification**: Milestone G5 validated migration and full application integration against local PostgreSQL 17 (`neuroroute_test`). Production deployment cutover is deliberately deferred to Milestone G6.
- **42 Baseline Records**: All 42 baseline records (9 workspaces, 24 model slots, 2 test runs, 7 requests) were verified intact in PostgreSQL with 100% equivalence, while source `neuroroute.db` remained read-only and unmodified.

## Milestone G6: Production Readiness Audit and Deployment Blueprint

| # | Decision | Replaces | Why |
|---|---|---|---|
| D-55 | **Two-tier decoupled rollback strategy.** | Coupled database rollbacks or destructive migrations | Explicitly decouples application traffic rollback (instant environment variable / deployment revert to previous container/commit) from database rollback (managed PITR snapshot restore). Because SQLite `neuroroute.db` is an immutable offline archive, traffic can be rolled back instantaneously with zero database schema rollback risk. |
| D-56 | **Least-privilege production credentials and separate secret lifecycle.** | Reusing local database passwords or superuser accounts | Production PostgreSQL must use a dedicated, least-privilege user (`neuroroute_app`) with permissions restricted to the application database, with mandatory TLS (`sslmode=require`). Existing `NEUROROUTE_SECRET_KEY` must be preserved to maintain decryptability of migrated BYOK credentials. |

### Limitations & Honest Notes (Milestone G6)
- **Zero Production Mutations**: Milestone G6 is an audit and planning phase. No production infrastructure was provisioned, no environment variables were altered, no traffic was shifted, and no deployment was triggered.
- **In-Memory Rate Limiting**: The 20 calls/min live BYOK rate limiter remains in-memory per process. If horizontal autoscaling (multiple uvicorn pods or serverless instances) is introduced in production, a shared Redis instance should back the rate limiter to enforce cluster-wide quotas.

## Milestone G7.3: Browser End-to-End Test Suite

| # | Decision | Replaces | Why |
|---|---|---|---|
| D-57 | **Dedicated `tests/browser/` package with ephemeral Uvicorn fixture and clean optional dependency skipping.** | Embedding browser tests in unit suites or requiring heavyweight dependencies | Allows the 119 unit/integration tests to execute fast and offline without external browser binaries. The browser suite uses `pytest.mark.skipif` when `playwright` is not installed and spins up an isolated Uvicorn server on an ephemeral SQLite DB in `tmp_path`, guaranteeing zero mutations to `neuroroute.db` or PostgreSQL. |

### Limitations & Honest Notes (Milestone G7.3)
- **Browser Binary Availability**: Running the browser E2E suite requires installing `playwright` (`pip install playwright`). Because Google Chrome and Microsoft Edge are already installed locally on the system, Playwright can execute directly via `channel="chrome"` or `channel="msedge"` without separate binary downloads once the Python binding is installed.

## Milestone G7.10: Defense-in-Depth CSRF Protection and Documentation Consistency

| # | Decision | Replaces | Why |
|---|---|---|---|
| D-58 | **Multi-layered CSRF defense-in-depth with Fetch Metadata, Origin/Referer validation, and conditional trusted-proxy host resolution.** | Simple Origin-only check that skipped validation when Origin header was absent | Protects state-changing endpoints (`POST`, `PUT`, `PATCH`, `DELETE` on `/api/workspace/*`, `/api/models/*`, `/models/*`, and `/route`) across multiple layers: (1) Rejects requests where `Sec-Fetch-Site` is explicitly `cross-site`; (2) Validates `Origin` against `Host` (and against `X-Forwarded-Host` only if `TRUST_PROXY_HEADERS=1`); (3) Validates `Referer` against `Host` if `Origin` is missing; (4) Rejects cookie-authenticated body-less mutations when `Origin`, `Referer`, and valid same-origin Fetch Metadata are all absent; (5) Enforces `Content-Type: application/json` for requests with payloads; (6) Preserves unauthenticated CLI requests (e.g. `POST /route` with JSON payload and no cookies). |

### Limitations & Honest Notes (Milestone G7.10)
- **Reverse Proxy Header Trust**: `X-Forwarded-Host` is intentionally ignored by default to prevent attackers from spoofing host headers to bypass Origin validation. It is trusted only when `TRUST_PROXY_HEADERS=1` is explicitly configured in a secured reverse proxy deployment (such as behind Nginx, Cloudflare, or Vercel).
- **Third-Party Dependency Warnings**: Deprecation warnings originating from Starlette's per-request `cookies=` parameter in `test_sliding_expiration.py` were eliminated by setting cookies directly on client instances (`client.cookies.set(...)`). Two remaining deprecation warnings (`websockets.legacy` and `WebSocketServerProtocol`) stem from Uvicorn's internal websocket protocol imports under Python 3.14 and are deferred until upstream Uvicorn releases update their websocket protocol handlers.

## Milestone G7.11.1: PostgreSQL Least-Privilege Implementation & Startup Separation

| # | Decision | Replaces | Why |
|---|---|---|---|
| D-59 | **Three-tier PostgreSQL least-privilege role boundaries and decoupled startup readiness check.** | Monolithic database roles and auto-migrating application startup | Decouples application execution from schema creation. (1) Normal gateway runtime (`neuroroute_app`) operates strictly under DML permissions (`SELECT, INSERT, UPDATE` on `workspaces`; `SELECT, INSERT, UPDATE, DELETE` on `model_slots`; `SELECT, INSERT` on `model_test_runs` and `requests`; `SELECT` on `schema_migrations`; `USAGE, SELECT` on sequences) with zero DDL privileges (`CREATE/ALTER/DROP`). (2) Application startup runs a read-only readiness check (`check_postgres_schema_readiness()`), aborting clearly with `SchemaNotReadyError` without ever falling back to SQLite or requiring DDL permissions. (3) Migrations run out-of-band under `neuroroute_admin` (`python -m src.gateway.migrator` or `AUTO_MIGRATE=1`). (4) Maintenance runs under `neuroroute_cleanup` (`scripts/cleanup_expired_workspaces.py` with `CLEANUP_DATABASE_URL`), restricted to batch row deletions and request anonymization with zero `INSERT`, sequence, or DDL access. |

### Limitations & Honest Notes (Milestone G7.11.1)
- **Safe Disposable Testing Only**: All least-privilege tests (`tests/test_postgres_least_privilege.py`) ran against a dynamically created disposable database (`neuroroute_lp_test`) and ephemeral test roles. Existing production/local databases `neuroroute.db` (42 records) and `neuroroute_test` (42 records) remained untouched with zero role, grant, or data mutations.
- **Out-of-Band Migration Workflow**: In production deployments following least-privilege practices, schema migrations must be applied as a pre-deployment step (or CI/CD migration job) using `python -m src.gateway.migrator` with admin credentials (`MIGRATION_DATABASE_URL`) before launching gateway pods under runtime credentials (`DATABASE_URL`).
