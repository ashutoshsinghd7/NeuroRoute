
"""Tests for CP4: NeuroRoute hard filter."""

from types import SimpleNamespace

import pytest

from src.gateway.filter import check, filter_models


@pytest.fixture
def request_data():
    """Create a fake request with fixed hard limits."""
    return SimpleNamespace(
        max_latency_ms=2000,
        max_cost=0.10,
    )


@pytest.fixture
def model():
    """Create a fake model that passes all hard constraints."""
    return SimpleNamespace(
        name="test-model",
        status="up",
        context_limit=32000,
        capability=3,
        latency_ms=500,
    )


@pytest.mark.parametrize(
    ("changes", "needed_context", "est_cost", "min_cap", "expected"),
    [
        # Rule 1: unavailable model
        (
            {"status": "down"},
            8000, 0.05, 3,
            "model is down",
        ),
        # Rule 2: insufficient context
        (
            {},
            64000, 0.05, 3,
            "context too small",
        ),
        # Rule 3: insufficient capability
        (
            {"capability": 2},
            8000, 0.05, 3,
            "capability 2 < 3",
        ),
        # Rule 4: latency exceeds the limit
        (
            {"latency_ms": 2500},
            8000, 0.05, 3,
            "latency 2500 > 2000",
        ),
        # Rule 5: cost exceeds the budget
        (
            {},
            8000, 0.15, 3,
            "cost 0.15 > 0.1",
        ),
    ],
)
def test_rejection_rules(
    model,
    request_data,
    changes,
    needed_context,
    est_cost,
    min_cap,
    expected,
):
    """Each hard rule rejects an ineligible model."""
    for field, value in changes.items():
        setattr(model, field, value)

    reason = check(
        model=model,
        req=request_data,
        needed_context=needed_context,
        est_cost=est_cost,
        min_cap=min_cap,
    )

    assert reason == expected


def test_accepts_valid_model(model, request_data):
    """A model that passes every rule is accepted."""
    reason = check(
        model=model,
        req=request_data,
        needed_context=8000,
        est_cost=0.05,
        min_cap=3,
    )

    assert reason is None


def test_filter_separates_valid_and_rejected_models(
    model, request_data
):
    """The filter returns valid models and rejection reasons."""
    slow_model = SimpleNamespace(
        name="slow-model",
        status="up",
        context_limit=32000,
        capability=4,
        latency_ms=3000,
    )

    models = [model, slow_model]
    costs = {
        "test-model": 0.05,
        "slow-model": 0.04,
    }

    valid_models, rejected = filter_models(
        models=models,
        req=request_data,
        needed_context=8000,
        estimated_costs=costs,
        min_cap=3,
    )

    assert valid_models == [model]
    assert rejected == [
        (slow_model, "latency 3000 > 2000")
    ]


def test_first_failure_reason_wins(model, request_data):
    """Only the first failed rule is returned."""
    model.status = "down"
    model.context_limit = 100
    model.capability = 1
    model.latency_ms = 5000

    reason = check(
        model=model,
        req=request_data,
        needed_context=8000,
        est_cost=0.50,
        min_cap=5,
    )

    assert reason == "model is down"


def test_missing_cost_estimate_raises_clear_error(request_data):
    """A missing CP2 estimate must not be silently ignored."""
    candidate = SimpleNamespace(
        name="unpriced-model",
        status="up",
        context_limit=32000,
        capability=3,
        latency_ms=500,
    )

    with pytest.raises(
        ValueError,
        match="Missing estimated cost for model: unpriced-model",
    ):
        filter_models(
            models=[candidate],
            req=request_data,
            needed_context=8000,
            estimated_costs={},
            min_cap=3,
        )
