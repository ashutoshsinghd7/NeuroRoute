"""The whole routing pipeline in one readable function (no web framework in here).

Order:  tokens -> classifier -> HARD FILTER -> rank -> call + failover -> response with trace.
Rules come first. The AI estimate can only RAISE the needed capability, never lower it.
"""
from src.classifier.predict import classify
from src.gateway.failover import run_with_failover
from src.gateway.filter import hard_filter
from src.gateway.ranking import rank
from src.gateway.tokens import estimate_tokens
from src.models.simulated import call_model


def _refuse(message, trace):
    rejected = [{"model": c["model"], "reason": c["reason"]} for c in trace["considered"] if not c["valid"]]
    trace["rejected"] = rejected
    return {"status": "NO_VALID_MODEL", "message": message, "answer": None,
            "chosen_model": None, "rejected": rejected, "trace": trace}


async def route_request(req, models, call=None):
    tokens = estimate_tokens(req.prompt)

    # 1) AI estimate (hardcoded rules by default; see src/classifier/predict.py)
    cx = classify(req.prompt)

    # 2) The estimate may only RAISE the needed capability.
    min_cap = max(req.min_capability, cx["level"])
    valid, considered = hard_filter(models, req, tokens, min_cap)

    # 3) If raising made the list empty, fall back to the user's own minimum and flag it.
    #    Cost, latency and context limits are NEVER relaxed.
    if not valid and min_cap > req.min_capability:
        min_cap = req.min_capability
        valid, considered = hard_filter(models, req, tokens, min_cap)
        cx["low_confidence_routing"] = True
    cx["min_capability_used"] = min_cap

    trace = {"complexity": cx, "tokens": tokens, "considered": considered,
             "attempts": [], "cost": 0.0, "total_latency_ms": 0}

    if not valid:
        return _refuse("No model satisfies all constraints. Nothing was sent.", trace)

    # 4) Rank, then call with failover (only from the valid list).
    ranked = rank(valid, req.preference)
    chosen, answer, attempts, total = await run_with_failover(
        ranked, req.prompt, req.max_latency_ms, call or call_model)
    trace["attempts"], trace["total_latency_ms"] = attempts, total

    if chosen is None:
        return _refuse("Every valid model failed or ran out of time. No constraint was relaxed.", trace)

    trace["cost"] = chosen["est_cost"]
    trace["rejected"] = [{"model": c["model"], "reason": c["reason"]} for c in considered if not c["valid"]]
    return {"status": "OK", "message": "Routed.", "answer": answer,
            "chosen_model": chosen["name"], "trace": trace}
