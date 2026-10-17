"""Pytest fixtures for browser end-to-end tests.
Strictly isolates execution:
- Uses an ephemeral SQLite database in pytest tmp_path.
- Never touches neuroroute.db or PostgreSQL target.
- Boots an isolated Uvicorn test server on an ephemeral port.
- Gracefully skips when playwright is not installed.
"""
import os
import socket
import threading
import time
from typing import Generator
import pytest
from cryptography.fernet import Fernet

try:
    from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False


def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line("markers", "browser: marks browser end-to-end tests")


def find_free_port() -> int:
    """Find an available TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def isolated_server(tmp_path_factory) -> Generator[str, None, None]:
    """Start an isolated Uvicorn server against a dedicated temporary SQLite database.
    Guarantees that neither neuroroute.db nor neuroroute_test is touched.
    """
    import uvicorn
    from src.gateway.main import app

    temp_dir = tmp_path_factory.mktemp("browser_e2e")
    test_db_path = str(temp_dir / "browser_test.db")
    
    # Configure isolated environment
    original_env = os.environ.copy()
    os.environ["NEUROROUTE_DB"] = test_db_path
    os.environ.pop("DATABASE_URL", None)  # Ensure SQLite backend
    os.environ["NEUROROUTE_SECRET_KEY"] = Fernet.generate_key().decode()
    os.environ.pop("ENVIRONMENT", None)
    os.environ.pop("VERCEL", None)

    port = find_free_port()
    config = uvicorn.Config(
        app=app,
        host="127.0.0.1",
        port=port,
        log_level="error",
        loop="asyncio",
    )
    server = uvicorn.Server(config)
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()

    base_url = f"http://127.0.0.1:{port}"

    # Poll until server is ready
    import urllib.request
    start_time = time.monotonic()
    ready = False
    while time.monotonic() - start_time < 15.0:
        try:
            with urllib.request.urlopen(f"{base_url}/", timeout=1.0) as resp:
                if resp.status == 200:
                    ready = True
                    break
        except Exception:
            time.sleep(0.1)

    if not ready:
        server.should_exit = True
        server_thread.join(timeout=2.0)
        raise RuntimeError(f"Isolated test server failed to start on {base_url} within 15s")

    yield base_url

    # Graceful shutdown
    server.should_exit = True
    server_thread.join(timeout=5.0)

    # Restore environment
    os.environ.clear()
    os.environ.update(original_env)


@pytest.fixture(scope="module")
def browser_instance():
    """Launch headless browser instance using installed Chromium, Chrome, or Edge."""
    if not HAS_PLAYWRIGHT:
        pytest.skip("playwright is required for browser e2e tests")

    with sync_playwright() as p:
        browser = None
        for channel in ["chrome", "msedge", None]:
            try:
                kwargs = {
                    "headless": True,
                    "args": ["--disable-gpu", "--no-sandbox"],
                }
                if channel:
                    kwargs["channel"] = channel
                browser = p.chromium.launch(**kwargs)
                break
            except Exception:
                continue

        if browser is None:
            pytest.skip("No compatible browser binary found. Run 'playwright install chromium'.")

        yield browser
        browser.close()


_TAILWIND_SCRIPT: bytes = b""


def _get_tailwind_script() -> bytes:
    global _TAILWIND_SCRIPT
    if not _TAILWIND_SCRIPT:
        try:
            req = urllib.request.Request(
                "https://cdn.tailwindcss.com?plugins=forms,container-queries",
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                _TAILWIND_SCRIPT = resp.read()
        except Exception:
            _TAILWIND_SCRIPT = b""
    return _TAILWIND_SCRIPT


@pytest.fixture
def browser_context(browser_instance):
    """Provide a fresh, isolated browser context with clear cookies and storage."""
    context = browser_instance.new_context(
        viewport={"width": 1280, "height": 800},
        ignore_https_errors=True,
    )
    # Fulfill external font requests to eliminate network jitter during automated test runs
    context.route("https://fonts.googleapis.com/**", lambda route: route.fulfill(status=200, body=""))
    context.route("https://fonts.gstatic.com/**", lambda route: route.fulfill(status=200, body=""))
    tw = _get_tailwind_script()
    if tw:
        context.route(
            "https://cdn.tailwindcss.com/**",
            lambda route: route.fulfill(status=200, content_type="application/javascript", body=tw),
        )
    yield context
    try:
        context.close()
    except Exception:
        pass
    time.sleep(0.3)


@pytest.fixture
def page(browser_context, isolated_server: str):
    """Provide a browser page pre-navigated to the isolated server."""
    p = browser_context.new_page()
    try:
        p.goto(isolated_server, wait_until="domcontentloaded", timeout=15000)
    except Exception:
        time.sleep(1.0)
        p.goto(isolated_server, wait_until="domcontentloaded", timeout=30000)
    # Wait for gateway online indicator
    p.wait_for_selector("#connLabel:has-text('gateway online')", timeout=10000)
    yield p
    try:
        p.close()
    except Exception:
        pass
