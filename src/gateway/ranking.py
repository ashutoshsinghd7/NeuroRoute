
"""CP5: Rank models that have already passed the CP4 hard filter."""

SUPPORTED_PREFERENCES = {"cheapest", "fastest", "highest_quality"}


def rank_models(valid_models, preference, estimated_costs):
    """Return a new, ordered list of CP4-approved models.

    cheapest       -> estimated cost, ascending
    fastest        -> latency, ascending
    highest_quality -> capability, descending

    Ties are broken by lower failure_rate first.
    """

    if preference not in SUPPORTED_PREFERENCES:
        raise ValueError(
            f"Unsupported preference: {preference!r}. "
            f"Expected one of {sorted(SUPPORTED_PREFERENCES)}."
        )

    if preference == "cheapest":
        for model in valid_models:
            if model.name not in estimated_costs:
                raise ValueError(
                    f"Missing estimated cost for model: {model.name}"
                )

        key = lambda model: (
            estimated_costs[model.name],
            model.failure_rate,
        )

    elif preference == "fastest":
        key = lambda model: (
            model.latency_ms,
            model.failure_rate,
        )

    else:  # highest_quality
        key = lambda model: (
            -model.capability,
            model.failure_rate,
        )

    # Never modify CP4's original list.
    return sorted(valid_models, key=key)
