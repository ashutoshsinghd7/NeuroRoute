"""RANKING (CP5). Sorts ONLY the already-valid models. Ties: lower failure_rate first."""

KEYS = {
    "cheapest":        lambda m: (m["est_cost"], m["failure_rate"]),
    "fastest":         lambda m: (m["latency_ms"], m["failure_rate"]),
    "highest_quality": lambda m: (-m["capability"], m["failure_rate"]),
}


def rank(valid_models, preference):
    return sorted(valid_models, key=KEYS[preference])
