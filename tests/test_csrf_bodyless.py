"""Tests for CSRF Defense-in-Depth, Fetch Metadata, Origin/Referer validation, and Proxy safety."""
import pytest
from fastapi.testclient import TestClient

from src.gateway.db import init_workspace_schema
from src.gateway.logger import init_db
from src.gateway.main import app


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path, monkeypatch):
    """Isolate database to a fresh temporary SQLite file for each test."""
    db_file = str(tmp_path / "test_csrf.db")
    monkeypatch.setenv("NEUROROUTE_DB", db_file)
    init_db()
    init_workspace_schema()
    yield db_file


@pytest.fixture
def client():
    """FastAPI TestClient fixture with default same-origin metadata."""
    return TestClient(app)


@pytest.fixture
def raw_client():
    """FastAPI TestClient without default Fetch Metadata headers."""
    return TestClient(app, headers={})


# ===========================================================================
# 1. Baseline bodyless & Content-Type tests (Preserved)
# ===========================================================================

def test_bodyless_post_reset_succeeds_same_origin(client, load_demo):
    """Body-less POST /api/models/reset succeeds without Content-Type header when same-origin."""
    load_demo(client)
    resp = client.post("/api/models/reset")
    assert resp.status_code == 200
    models = resp.json()
    assert isinstance(models, list)
    assert len(models) == 0


def test_bodyless_patch_toggle_succeeds_same_origin(client, load_demo):
    """Body-less PATCH /api/workspace/models/{slot}/toggle succeeds without Content-Type header."""
    load_demo(client)
    resp = client.patch("/api/workspace/models/1/toggle")
    assert resp.status_code == 200
    model = resp.json()
    assert model["enabled"] is False


def test_foreign_origin_rejected_on_state_changing_requests(client):
    """A request with a foreign Origin header is rejected with 403 CSRF_FAILED."""
    client.get("/api/workspace/models")

    # Foreign origin on body-less POST
    resp_reset = client.post(
        "/api/models/reset",
        headers={"Origin": "https://malicious-site.com"},
    )
    assert resp_reset.status_code == 403
    assert resp_reset.json()["error"]["code"] == "CSRF_FAILED"

    # Foreign origin on body-less PATCH
    resp_toggle = client.patch(
        "/api/workspace/models/1/toggle",
        headers={"Origin": "https://malicious-site.com"},
    )
    assert resp_toggle.status_code == 403
    assert resp_toggle.json()["error"]["code"] == "CSRF_FAILED"

    # Foreign origin on /route
    resp_route = client.post(
        "/route",
        json={"prompt": "test", "max_latency_ms": 1000, "max_cost": 1.0, "min_capability": 1},
        headers={"Origin": "https://malicious-site.com"},
    )
    assert resp_route.status_code == 403
    assert resp_route.json()["error"]["code"] == "CSRF_FAILED"


def test_body_with_wrong_content_type_rejected(client):
    """A request carrying a body with non-JSON Content-Type is rejected with 415."""
    client.get("/api/workspace/models")

    # Send state-changing request with body and text/plain content-type
    resp = client.post(
        "/api/workspace/models/1",
        content="some plain text body",
        headers={"Content-Type": "text/plain"},
    )
    assert resp.status_code == 415
    assert resp.json()["error"]["code"] == "UNSUPPORTED_MEDIA_TYPE"

    # Send state-changing request with application/x-www-form-urlencoded
    resp2 = client.post(
        "/route",
        content="prompt=hello&max_latency_ms=1000",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert resp2.status_code == 415
    assert resp2.json()["error"]["code"] == "UNSUPPORTED_MEDIA_TYPE"


# ===========================================================================
# 2. Fetch Metadata (Sec-Fetch-Site) Tests
# ===========================================================================

def test_sec_fetch_site_cross_site_rejected(client, load_demo):
    """Sec-Fetch-Site: cross-site is explicitly rejected with 403 CSRF_FAILED."""
    load_demo(client)

    # Cross-site body-less POST
    resp1 = client.post(
        "/api/models/reset",
        headers={"Sec-Fetch-Site": "cross-site"},
    )
    assert resp1.status_code == 403
    assert resp1.json()["error"]["code"] == "CSRF_FAILED"
    assert "Fetch Metadata" in resp1.json()["error"]["message"]

    # Cross-site mutation with JSON body
    resp2 = client.post(
        "/route",
        json={"prompt": "hello", "max_latency_ms": 1000, "max_cost": 1.0, "min_capability": 1},
        headers={"Sec-Fetch-Site": "cross-site"},
    )
    assert resp2.status_code == 403
    assert resp2.json()["error"]["code"] == "CSRF_FAILED"


def test_sec_fetch_site_same_origin_accepted(client, load_demo):
    """Sec-Fetch-Site: same-origin allows legitimate requests to succeed."""
    load_demo(client)
    resp = client.post(
        "/api/models/reset",
        headers={"Sec-Fetch-Site": "same-origin"},
    )
    assert resp.status_code == 200
    assert resp.json() == []


def test_sec_fetch_site_same_site_accepted(client, load_demo):
    """Sec-Fetch-Site: same-site allows legitimate same-site requests."""
    load_demo(client)
    resp = client.patch(
        "/api/workspace/models/1/toggle",
        headers={"Sec-Fetch-Site": "same-site"},
    )
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False


# ===========================================================================
# 3. Referer Fallback & Validation Tests
# ===========================================================================

def test_missing_origin_foreign_referer_rejected_authenticated(raw_client, load_demo):
    """Missing Origin with a foreign Referer is rejected for cookie-authenticated requests."""
    load_demo(raw_client)
    assert raw_client.cookies.get("nr_session") is not None

    resp = raw_client.post(
        "/api/models/reset",
        headers={"Referer": "https://malicious-site.com/evil-page"},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "CSRF_FAILED"
    assert "Referer header does not match Host" in resp.json()["error"]["message"]


def test_missing_origin_valid_referer_accepted_authenticated(raw_client, load_demo):
    """Missing Origin with a valid same-origin Referer is accepted for cookie-authenticated requests."""
    load_demo(raw_client)
    assert raw_client.cookies.get("nr_session") is not None

    resp = raw_client.post(
        "/api/models/reset",
        headers={"Referer": "http://testserver/dashboard"},
    )
    assert resp.status_code == 200
    assert resp.json() == []


# ===========================================================================
# 4. Body-less Cookie-Authenticated Requests with All Verification Absent
# ===========================================================================

def test_cookie_authenticated_bodyless_rejected_when_origin_referer_metadata_absent(raw_client, load_demo):
    """Cookie-authenticated body-less mutation with Origin, Referer, and usable Fetch Metadata all absent is rejected."""
    # Establish cookie
    resp_init = raw_client.get("/api/workspace/models")
    assert resp_init.status_code == 200
    cookie_token = raw_client.cookies.get("nr_session")
    assert cookie_token is not None

    # raw_client sends NO Origin, NO Referer, and NO Sec-Fetch-Site
    resp = raw_client.post("/api/models/reset")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "CSRF_FAILED"
    assert "Missing origin verification" in resp.json()["error"]["message"]

    # Body-less PATCH toggle is similarly rejected
    resp_toggle = raw_client.patch("/api/workspace/models/1/toggle")
    assert resp_toggle.status_code == 403
    assert resp_toggle.json()["error"]["code"] == "CSRF_FAILED"


# ===========================================================================
# 5. Unauthenticated CLI/API Compatibility
# ===========================================================================

def test_unauthenticated_cli_json_post_route_succeeds(raw_client):
    """Legitimate unauthenticated JSON CLI requests (e.g. curl /route) remain functional without session cookies."""
    # Ensure no session cookie is attached
    assert raw_client.cookies.get("nr_session") is None

    req = {
        "prompt": "CLI user testing route",
        "max_latency_ms": 2000,
        "max_cost": 1.0,
        "min_capability": 1,
        "required_context": 100,
        "preference": "fastest",
    }
    resp = raw_client.post("/route", json=req)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "OK"
    assert "chosen_model" in data


# ===========================================================================
# 6. Malformed Origin and Referer Handling
# ===========================================================================

def test_malformed_origin_and_referer_handled_safely(raw_client, load_demo):
    """Malformed Origin and Referer values are safely handled and rejected with 403."""
    load_demo(raw_client)

    # Malformed Origin: "null"
    resp_null = raw_client.post(
        "/api/models/reset",
        headers={"Origin": "null"},
    )
    assert resp_null.status_code == 403
    assert resp_null.json()["error"]["code"] == "CSRF_FAILED"

    # Malformed Origin: invalid scheme/syntax
    resp_bad_orig = raw_client.post(
        "/api/models/reset",
        headers={"Origin": "://bad-uri"},
    )
    assert resp_bad_orig.status_code == 403
    assert resp_bad_orig.json()["error"]["code"] == "CSRF_FAILED"

    # Malformed Referer
    resp_bad_ref = raw_client.post(
        "/api/models/reset",
        headers={"Referer": "not-a-valid-referer-url"},
    )
    assert resp_bad_ref.status_code == 403
    assert resp_bad_ref.json()["error"]["code"] == "CSRF_FAILED"


# ===========================================================================
# 7. Reverse Proxy Safety & Trusted Proxy Configuration
# ===========================================================================

def test_arbitrary_x_forwarded_host_cannot_bypass_origin_check(raw_client, load_demo, monkeypatch):
    """By default, arbitrary X-Forwarded-Host headers are not trusted and cannot bypass Origin validation."""
    monkeypatch.delenv("TRUST_PROXY_HEADERS", raising=False)
    load_demo(raw_client)

    # Attacker tries to match their malicious origin via forged X-Forwarded-Host
    resp = raw_client.post(
        "/api/models/reset",
        headers={
            "Origin": "https://attacker.example.com",
            "X-Forwarded-Host": "attacker.example.com",
        },
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "CSRF_FAILED"


def test_trusted_proxy_forwarded_host_accepted_when_configured(raw_client, load_demo, monkeypatch):
    """When TRUST_PROXY_HEADERS is explicitly enabled, forwarded host is trusted for valid reverse proxies."""
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "1")
    load_demo(raw_client)

    # Legitimate reverse proxy passes client-facing host
    resp = raw_client.post(
        "/api/models/reset",
        headers={
            "Origin": "https://neuroroute.production.com",
            "X-Forwarded-Host": "neuroroute.production.com",
        },
    )
    assert resp.status_code == 200
    assert resp.json() == []
