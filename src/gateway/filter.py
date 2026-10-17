"""HARD FILTER (CP4). Plain Python, no AI. Decides who is ALLOWED.

A model that breaks even one rule is rejected, with the FIRST failing reason.
"""
from src.gateway.tokens import estimate_cost, needed_context


def check(model, need_ctx, est_cost, min_cap, max_latency_ms, max_cost):
    """Return None if the model is valid, otherwise a short reason string."""
    if not model.get("enabled", True):
        return "model is disabled"
    if model["status"] != "up":
        return "model is down"
    if model["context_limit"] < need_ctx:
        return f"context {model['context_limit']} < {need_ctx}"
    if model["capability"] < min_cap:
        return f"capability {model['capability']} < {min_cap}"
    if model["latency_ms"] > max_latency_ms:
        return f"latency {model['latency_ms']} > {max_latency_ms}"
    if est_cost > max_cost:
        return f"cost {est_cost:g} > {max_cost:g}"
    return None


def hard_filter(models, req, tokens, min_cap):
    """Return (valid_models, considered). `considered` has one entry per model, with a reason."""
    need_ctx = needed_context(req.required_context, tokens)
    valid, considered = [], []
    for m in models:
        cost = estimate_cost(tokens, m["cost_per_1k"])
        reason = check(m, need_ctx, cost, min_cap, req.max_latency_ms, req.max_cost)
        considered.append({"model": m["name"], "valid": reason is None, "reason": reason,
                           "est_cost": cost, "latency_ms": m["latency_ms"]})
        if reason is None:
            valid.append({**m, "est_cost": cost})
    return valid, considered
