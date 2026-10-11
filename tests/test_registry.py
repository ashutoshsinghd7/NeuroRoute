"""CP0 tests: the registry loads and bad data is rejected."""
import json

import pytest
from pydantic import ValidationError

from src.gateway.schemas import load_registry


def test_registry_loads_all_models():
    models = load_registry()
    assert len(models) == 6
    assert {m.name for m in models} >= {"copilot", "gpt-5", "claude-sonnet-5.5", "chatgpt-3"}


def test_registry_has_all_capability_levels():
    levels = {m.capability for m in load_registry()}
    assert levels == {1, 2, 3, 4, 5}


def test_all_models_start_up():
    assert all(m.status == "up" for m in load_registry())


def test_duplicate_names_rejected(tmp_path):
    bad = tmp_path / "r.json"
    one = {"name": "a", "capability": 1, "context_limit": 100, "cost_per_1k": 0.1,
           "latency_ms": 100, "failure_rate": 0.0, "status": "up"}
    bad.write_text(json.dumps([one, one]))
    with pytest.raises(ValueError):
        load_registry(bad)


def test_bad_capability_rejected(tmp_path):
    bad = tmp_path / "r.json"
    bad.write_text(json.dumps([{"name": "a", "capability": 9, "context_limit": 100,
                                "cost_per_1k": 0.1, "latency_ms": 100,
                                "failure_rate": 0.0, "status": "up"}]))
    with pytest.raises(ValidationError):
        load_registry(bad)
