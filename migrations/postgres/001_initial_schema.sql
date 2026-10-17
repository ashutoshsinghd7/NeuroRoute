-- Migration 001: Initial PostgreSQL Schema for NeuroRoute
-- Creates equivalent tables for workspaces, model_slots, model_test_runs, and requests.
-- Preserves all primary keys, foreign keys with cascade/set null rules, constraints, defaults, and indexes.

-- 1. Workspaces
CREATE TABLE IF NOT EXISTS workspaces (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_token_hash VARCHAR(64) NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_workspaces_token_hash ON workspaces(session_token_hash);
CREATE INDEX IF NOT EXISTS idx_workspaces_expires_at ON workspaces(expires_at);

-- 2. Model Slots (maximum 6 slots per workspace)
CREATE TABLE IF NOT EXISTS model_slots (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    workspace_id BIGINT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    slot_number SMALLINT NOT NULL CHECK (slot_number BETWEEN 1 AND 6),
    model_name VARCHAR(100) NOT NULL,
    provider VARCHAR(50) NOT NULL DEFAULT 'simulated',
    model_id VARCHAR(100) NOT NULL,
    capability SMALLINT NOT NULL CHECK (capability BETWEEN 1 AND 5),
    context_limit INTEGER NOT NULL CHECK (context_limit > 0),
    cost_per_1k DOUBLE PRECISION NOT NULL CHECK (cost_per_1k >= 0),
    latency_ms INTEGER NOT NULL CHECK (latency_ms > 0),
    failure_rate DOUBLE PRECISION NOT NULL DEFAULT 0 CHECK (failure_rate BETWEEN 0 AND 1),
    status VARCHAR(10) NOT NULL DEFAULT 'up' CHECK (status IN ('up', 'down')),
    enabled SMALLINT NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    execution_mode VARCHAR(20) NOT NULL DEFAULT 'simulated' CHECK (execution_mode IN ('simulated', 'live')),
    api_key_encrypted TEXT NULL,
    key_last4 VARCHAR(4) NULL,
    cost_per_1k_input DOUBLE PRECISION NULL,
    cost_per_1k_output DOUBLE PRECISION NULL,
    max_output_tokens INTEGER NOT NULL DEFAULT 512 CHECK (max_output_tokens BETWEEN 1 AND 4096),
    status_reason TEXT NULL,
    last_measured_latency_ms INTEGER NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT uq_model_slots_workspace_slot UNIQUE (workspace_id, slot_number),
    CONSTRAINT uq_model_slots_workspace_name UNIQUE (workspace_id, model_name)
);

CREATE INDEX IF NOT EXISTS idx_slots_workspace ON model_slots(workspace_id);

-- 3. Model Test Runs
CREATE TABLE IF NOT EXISTS model_test_runs (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    workspace_id BIGINT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    slot_number SMALLINT NOT NULL CHECK (slot_number BETWEEN 1 AND 6),
    success SMALLINT NOT NULL CHECK (success IN (0, 1)),
    latency_ms INTEGER NOT NULL,
    message TEXT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_test_runs_workspace ON model_test_runs(workspace_id);

-- 4. Requests (Inference audit logs, refusals, and decision traces)
CREATE TABLE IF NOT EXISTS requests (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    time VARCHAR(20) NULL,
    prompt_preview TEXT NULL,
    constraints TEXT NULL,
    estimated_level SMALLINT NULL,
    confidence DOUBLE PRECISION NULL,
    chosen_model VARCHAR(100) NULL,
    status VARCHAR(20) NULL,
    total_latency INTEGER NULL,
    cost DOUBLE PRECISION NULL,
    attempts_json TEXT NULL,
    rejected_json TEXT NULL,
    workspace_id BIGINT NULL REFERENCES workspaces(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_requests_workspace_id ON requests(workspace_id, id);
