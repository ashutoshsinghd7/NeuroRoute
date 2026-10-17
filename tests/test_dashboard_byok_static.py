"""Static security and BYOK integration tests for Dashboard (Milestone E)."""
import re
import pytest
from fastapi.testclient import TestClient

from src.gateway.main import app


@pytest.fixture
def client():
    return TestClient(app)


def test_dashboard_references_byok_and_demo_endpoints(client):
    """The dashboard HTML must reference /api/workspace/providers and /api/workspace/load-demo-models."""
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.text

    assert "/api/workspace/providers" in html
    assert "/api/workspace/load-demo-models" in html


def test_dashboard_no_console_log_of_key_variables(client):
    """The dashboard must never output API keys, secrets, or passwords in console.log."""
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.text

    # Extract all console.log statements
    console_logs = re.findall(r'console\.log\([^)]*\)', html, re.IGNORECASE)
    for log in console_logs:
        log_lower = log.lower()
        assert "key" not in log_lower
        assert "secret" not in log_lower
        assert "token" not in log_lower
        assert "password" not in log_lower


def test_dashboard_escapes_model_name_provider_and_status_reason(client):
    """User-controlled and server-supplied fields must be safely sanitized with esc()."""
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.text

    # Verify esc() is used when rendering model name, provider, and status_reason
    assert "esc(m.name)" in html
    assert "esc(m.provider" in html
    assert "esc(m.status_reason)" in html
