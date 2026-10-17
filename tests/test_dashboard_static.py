"""Tests for C6: Dashboard static HTML content and security invariants."""
import re
import pytest
from fastapi.testclient import TestClient

from src.gateway.db import init_workspace_schema
from src.gateway.logger import init_db
from src.gateway.main import app


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path, monkeypatch):
    """Isolate database to a fresh temporary SQLite file for each test."""
    db_file = str(tmp_path / "test_dash.db")
    monkeypatch.setenv("NEUROROUTE_DB", db_file)
    init_db()
    init_workspace_schema()
    yield db_file


@pytest.fixture
def client():
    """FastAPI TestClient fixture."""
    return TestClient(app)


def test_dashboard_serves_html_and_required_elements(client):
    """GET / serves the dashboard with Model Manager, Request History, and Reset workspace."""
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.text

    # Required section titles and buttons
    assert "Model Manager" in html
    assert "Request History" in html
    assert "Reset workspace" in html


def test_dashboard_has_secure_api_key_input(client):
    """The dashboard has a secure API key input with type='password' and autocomplete='off',
    no key storage in local/sessionStorage, and required security and UX text."""
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.text

    # Page contains an API key input with type="password" and autocomplete="off"
    assert re.search(r'<input[^>]+type=["\']password["\'][^>]+autocomplete=["\']off["\']', html) or \
           re.search(r'<input[^>]+autocomplete=["\']off["\'][^>]+type=["\']password["\']', html)

    # HTML never contains localStorage or sessionStorage usage for keys
    assert "localstorage" not in html.lower()
    assert "sessionstorage" not in html.lower()

    # Contains required texts
    assert "Load demo models" in html
    assert "Reset workspace" in html
    assert "third-party" in html
    assert "Key saved" in html


def test_dashboard_does_not_use_hardcoded_seed_as_truth(client):
    """The dashboard fetches from /api/workspace/models and does not rely on hardcoded SEED fallback."""
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.text

    # Ensure fetchWorkspace() calls the real workspace models endpoint
    assert "/api/workspace/models" in html
    # Ensure SEED is not defined as state source of truth
    assert "var SEED =" not in html
    assert "const SEED =" not in html
