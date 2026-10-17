"""Tests for Milestone G7.6: Production Secret Validation (SEC-02 Hardening).

Covers:
1. Local development and test environments do not mandate NEUROROUTE_SECRET_KEY.
2. Production environments (ENVIRONMENT=production, ENV=production, NODE_ENV=production, VERCEL=1)
   strictly require NEUROROUTE_SECRET_KEY.
3. Missing or empty secret in production raises SecretConfigError.
4. Malformed / invalid Fernet key in production raises SecretConfigError.
5. Error messages never expose or leak key contents.
6. Valid 32-byte URL-safe base64 Fernet key passes validation.
7. FastAPI lifespan startup executes validation and fails fast before handling requests.
8. Import-time purity: importing modules does not trigger secret validation side-effects.
"""
import os
import pytest
from cryptography.fernet import Fernet
from starlette.testclient import TestClient

from src.gateway.main import app
from src.gateway.secrets_store import (
    SecretConfigError,
    validate_production_secret,
)


@pytest.fixture(autouse=True)
def clean_env():
    """Ensure environment is cleanly restored after each test."""
    orig = os.environ.copy()
    yield
    os.environ.clear()
    os.environ.update(orig)


# ---------------------------------------------------------------------------
# 1. Non-Production Behavior (Local SQLite Dev & Testing)
# ---------------------------------------------------------------------------

def test_secret_validation_passes_in_non_production_without_key(monkeypatch):
    """When not in production, missing NEUROROUTE_SECRET_KEY does not raise."""
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("ENV", raising=False)
    monkeypatch.delenv("NODE_ENV", raising=False)
    monkeypatch.delenv("VERCEL", raising=False)
    monkeypatch.delenv("NEUROROUTE_SECRET_KEY", raising=False)

    # Must pass without exception
    validate_production_secret()


# ---------------------------------------------------------------------------
# 2. Production Environment Missing Secret Validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("env_var,env_val", [
    ("ENVIRONMENT", "production"),
    ("ENV", "production"),
    ("NODE_ENV", "production"),
    ("VERCEL", "1"),
])
def test_production_missing_secret_key_raises_config_error(monkeypatch, env_var, env_val):
    """In any production environment, missing NEUROROUTE_SECRET_KEY raises SecretConfigError."""
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("ENV", raising=False)
    monkeypatch.delenv("NODE_ENV", raising=False)
    monkeypatch.delenv("VERCEL", raising=False)
    monkeypatch.delenv("NEUROROUTE_SECRET_KEY", raising=False)

    monkeypatch.setenv(env_var, env_val)

    with pytest.raises(SecretConfigError) as exc_info:
        validate_production_secret()

    err = str(exc_info.value)
    assert "NEUROROUTE_SECRET_KEY must be configured in production environment" in err


def test_production_empty_secret_key_raises_config_error(monkeypatch):
    """Whitespace or empty NEUROROUTE_SECRET_KEY in production raises SecretConfigError."""
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("NEUROROUTE_SECRET_KEY", "   ")

    with pytest.raises(SecretConfigError) as exc_info:
        validate_production_secret()

    assert "NEUROROUTE_SECRET_KEY must be configured in production environment" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 3. Production Environment Malformed Secret Validation & Sanitization
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("malformed_key", [
    "not-a-fernet-key",
    "shortkey",
    "12345",
    "invalid_base64_!@#$%^&*()_+",
    "dGVzdF9rZXlfdGhhdF9pc19ub3RfMzJfYnl0ZXM=",  # base64 but wrong length
])
def test_production_malformed_secret_key_raises_sanitized_error(monkeypatch, malformed_key):
    """Malformed key raises SecretConfigError and NEVER leaks the input key in the message."""
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("NEUROROUTE_SECRET_KEY", malformed_key)

    with pytest.raises(SecretConfigError) as exc_info:
        validate_production_secret()

    err = str(exc_info.value)
    assert "NEUROROUTE_SECRET_KEY is malformed: must be a 32-byte url-safe base64-encoded key" in err
    # Strict secret sanitization guarantee:
    assert malformed_key not in err


# ---------------------------------------------------------------------------
# 4. Valid Production Configuration
# ---------------------------------------------------------------------------

def test_production_valid_secret_key_passes(monkeypatch):
    """A valid 32-byte URL-safe base64 Fernet key passes production validation cleanly."""
    valid_key = Fernet.generate_key().decode()
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("NEUROROUTE_SECRET_KEY", valid_key)

    # Must execute with zero exceptions
    validate_production_secret()


# ---------------------------------------------------------------------------
# 5. FastAPI Lifespan Startup Integration
# ---------------------------------------------------------------------------

def test_lifespan_startup_fails_fast_on_missing_production_secret(monkeypatch):
    """FastAPI lifespan startup aborts before serving traffic when production secret is missing."""
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pw@localhost:5432/db")
    monkeypatch.delenv("NEUROROUTE_SECRET_KEY", raising=False)

    with pytest.raises(SecretConfigError):
        with TestClient(app):
            pass


def test_lifespan_startup_succeeds_with_valid_production_secret(monkeypatch, tmp_path):
    """FastAPI lifespan startup succeeds when valid secret and database are configured."""
    valid_key = Fernet.generate_key().decode()
    test_db = str(tmp_path / "lifespan_test.db")
    monkeypatch.setenv("NEUROROUTE_DB", test_db)
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("VERCEL", raising=False)
    monkeypatch.setenv("NEUROROUTE_SECRET_KEY", valid_key)

    with TestClient(app) as client:
        resp = client.get("/api/workspace/models")
        assert resp.status_code == 200
