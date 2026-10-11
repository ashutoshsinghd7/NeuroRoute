
"""CP4: Hard filter for NeuroRoute.

Rejects models that violate any hard constraint.
The first rejection reason is recorded for each invalid model.
"""


def check(model, req, needed_context, est_cost, min_cap):
    """Return the first rejection reason, or None when the model is valid."""

    # Rule 1: The model must be available.
    if model.status != "up":
        return "model is down"

    # Rule 2: The model must support the required context.
    if model.context_limit < needed_context:
        return "context too small"

    # Rule 3: The model must meet the required capability.
    if model.capability < min_cap:
        return f"capability {model.capability} < {min_cap}"

    # Rule 4: The model must fit the latency limit.
    if model.latency_ms > req.max_latency_ms:
        return f"latency {model.latency_ms} > {req.max_latency_ms}"

    # Rule 5: The estimated cost must fit the budget.
    if est_cost > req.max_cost:
        return f"cost {est_cost} > {req.max_cost}"

    # No rule was broken.
    return None


def filter_models(models, req, needed_context, estimated_costs, min_cap):
    """Return valid models and rejected (model, reason) pairs.

    Args:
        models: Candidate model objects from the model registry.
        req: Request object containing max_latency_ms and max_cost.
        needed_context: Required context calculated by CP2.
        estimated_costs: Dictionary mapping model names to estimated costs.
        min_cap: Minimum capability decided by the request and, later, CP3.

    Returns:
        A tuple containing:
        - valid_models: Models that pass every hard constraint.
        - rejected: (model, reason) tuples for rejected models.
    """

    valid_models = []
    rejected = []

    for model in models:
        # Every candidate must have a corresponding CP2 cost estimate.
        if model.name not in estimated_costs:
            raise ValueError(
                f"Missing estimated cost for model: {model.name}"
            )

        reason = check(
            model=model,
            req=req,
            needed_context=needed_context,
            est_cost=estimated_costs[model.name],
            min_cap=min_cap,
        )

        if reason is None:
            valid_models.append(model)
        else:
            rejected.append((model, reason))

    return valid_models, rejected
