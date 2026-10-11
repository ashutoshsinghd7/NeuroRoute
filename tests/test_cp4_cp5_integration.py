
"""Integration tests: fake CP0-CP2 inputs -> CP4 -> CP5."""

from types import SimpleNamespace

import pytest

from src.gateway.filter import filter_models
from src.gateway.ranking import rank_models


@pytest.fixture
def simulated_pipeline():
    """Simulate CP0 registry, CP1 request, and CP2 estimates."""

    models = [
        SimpleNamespace(
            name="model-a",
            status="up",
            context_limit=32000,
            capability=3,
            latency_ms=500,
            failure_rate=0.05,
        ),
        SimpleNamespace(
            name="model-b",
            status="up",
            context_limit=64000,
            capability=5,
            latency_ms=900,
            failure_rate=0.01,
        ),
        SimpleNamespace(
            name="model-c",
            status="up",
            context_limit=32000,
            capability=4,
            latency_ms=300,
            failure_rate=0.10,
        ),
        SimpleNamespace(
            name="model-d",
            status="down",
            context_limit=64000,
            capability=5,
            latency_ms=100,
            failure_rate=0.01,
        ),
    ]

    # Fake CP1 request constraints.
    request = SimpleNamespace(
        max_latency_ms=2000,
        max_cost=0.10,
        preference="cheapest",
    )

    # Fake CP2 output: required context and cost per model.
    needed_context = 8000
    estimated_costs = {
        "model-a": 0.05,
        "model-b": 0.20,
        "model-c": 0.08,
        "model-d": 0.02,
    }

    # CP3 is not implemented yet, so use a fixed capability threshold.
    min_cap = 3

    return models, request, needed_context, estimated_costs, min_cap


@pytest.mark.parametrize(
    ("preference", "expected_order"),
    [
        ("cheapest", ["model-a", "model-c"]),
        ("fastest", ["model-c", "model-a"]),
        ("highest_quality", ["model-c", "model-a"]),
    ],
)
def test_cp4_then_cp5_ranks_only_eligible_models(
    simulated_pipeline, preference, expected_order
):
    models, request, context, costs, min_cap = simulated_pipeline
    request.preference = preference

    # Actual CP4 filter.
    valid_models, rejected = filter_models(
        models=models,
        req=request,
        needed_context=context,
        estimated_costs=costs,
        min_cap=min_cap,
    )

    # Actual CP5 ranking receives only CP4-approved models.
    try_list = rank_models(
        valid_models=valid_models,
        preference=request.preference,
        estimated_costs=costs,
    )

    assert [m.name for m in try_list] == expected_order
    assert {m.name for m in valid_models} == {"model-a", "model-c"}
    assert {m.name for m, _ in rejected} == {"model-b", "model-d"}

    # Rejected models must never appear in CP5 output.
    assert not (
        {m.name for m in try_list}
        & {m.name for m, _ in rejected}
    )


def test_cp4_empty_output_means_cp5_has_no_candidates(simulated_pipeline):
    models, request, context, costs, _ = simulated_pipeline

    # Set a minimum capability no model can satisfy.
    valid_models, rejected = filter_models(
        models=models,
        req=request,
        needed_context=context,
        estimated_costs=costs,
        min_cap=6,
    )

    try_list = rank_models(
        valid_models=valid_models,
        preference="cheapest",
        estimated_costs=costs,
    )

    assert valid_models == []
    assert len(rejected) == len(models)
    assert try_list == []
