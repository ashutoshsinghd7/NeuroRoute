
"""Unit tests for CP5 model ranking."""

from types import SimpleNamespace

import pytest

from src.gateway.ranking import rank_models


@pytest.fixture
def models():
    return [
        SimpleNamespace(
            name="model-a",
            capability=3,
            latency_ms=500,
            failure_rate=0.05,
        ),
        SimpleNamespace(
            name="model-b",
            capability=5,
            latency_ms=900,
            failure_rate=0.01,
        ),
        SimpleNamespace(
            name="model-c",
            capability=4,
            latency_ms=300,
            failure_rate=0.10,
        ),
    ]


@pytest.fixture
def costs():
    return {
        "model-a": 0.05,
        "model-b": 0.20,
        "model-c": 0.08,
    }


def test_cheapest_first(models, costs):
    ranked = rank_models(models, "cheapest", costs)
    assert [m.name for m in ranked] == [
        "model-a", "model-c", "model-b"
    ]


def test_fastest_first(models, costs):
    ranked = rank_models(models, "fastest", costs)
    assert [m.name for m in ranked] == [
        "model-c", "model-a", "model-b"
    ]


def test_highest_quality_first(models, costs):
    ranked = rank_models(models, "highest_quality", costs)
    assert [m.name for m in ranked] == [
        "model-b", "model-c", "model-a"
    ]


@pytest.mark.parametrize(
    ("preference", "costs"),
    [
        ("cheapest", {"reliable": 0.05, "unreliable": 0.05}),
        ("fastest", {"reliable": 0.05, "unreliable": 0.05}),
        ("highest_quality", {"reliable": 0.05, "unreliable": 0.05}),
    ],
)
def test_lower_failure_rate_breaks_ties(preference, costs):
    reliable = SimpleNamespace(
        name="reliable",
        capability=4,
        latency_ms=500,
        failure_rate=0.01,
    )
    unreliable = SimpleNamespace(
        name="unreliable",
        capability=4,
        latency_ms=500,
        failure_rate=0.20,
    )

    ranked = rank_models([unreliable, reliable], preference, costs)

    assert [m.name for m in ranked] == ["reliable", "unreliable"]


def test_unsupported_preference_raises_error(models, costs):
    with pytest.raises(ValueError, match="Unsupported preference"):
        rank_models(models, "most_popular", costs)


def test_missing_cost_raises_error_for_cheapest(models):
    with pytest.raises(ValueError, match="Missing estimated cost"):
        rank_models(models, "cheapest", {})


def test_empty_list_returns_empty_list(costs):
    assert rank_models([], "cheapest", costs) == []


def test_original_list_is_not_modified(models, costs):
    original_order = [m.name for m in models]

    rank_models(models, "fastest", costs)

    assert [m.name for m in models] == original_order
