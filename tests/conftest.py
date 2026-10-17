"""Shared test fixtures for NeuroRoute tests."""
import os
import pytest
import starlette.testclient
from fastapi.testclient import TestClient

from src.gateway.main import app

# Ensure TestClient simulates same-origin browser interactions by default
# unless specific headers are explicitly provided by the test case.
_orig_client_init = starlette.testclient.TestClient.__init__


def _patched_client_init(self, *args, **kwargs):
    if "headers" not in kwargs or kwargs["headers"] is None:
        kwargs["headers"] = {"sec-fetch-site": "same-origin"}
    _orig_client_init(self, *args, **kwargs)


starlette.testclient.TestClient.__init__ = _patched_client_init


@pytest.fixture
def load_demo():
    """Helper fixture to populate demo models into a client's workspace."""
    def _loader(client: TestClient):
        return client.post("/api/workspace/load-demo-models")
    return _loader


@pytest.fixture
def demo_client(load_demo):
    """TestClient that has already established a session and loaded demo models."""
    client = TestClient(app)
    load_demo(client)
    return client
