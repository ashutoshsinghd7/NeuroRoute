"""Live call execution module for NeuroRoute BYOK workspace models.
Handles:
- Per-workspace rate limiting (20 live calls / min).
- Max prompt characters check (8000 chars).
- Live provider dispatch via src.providers.complete.
- Dynamic slot status / latency updates upon success or failure.
- Usage tracking for trace["usage"].
"""
from collections import defaultdict
import time
from typing import Any, Optional

from src.gateway.workspace_repo import (
    get_slot_connection_data,
    update_measured_latency,
    update_slot_status,
)
from src.models.simulated import call_model
from src.providers import complete
from src.providers.registry import ProviderError

MAX_PROMPT_CHARS = 8000
RATE_LIMIT_CALLS = 20
RATE_LIMIT_WINDOW_SECONDS = 60.0


class WorkspaceRateLimiter:
    """In-memory sliding window rate limiter per workspace."""
    def __init__(self):
        self._calls: dict[int, list[float]] = defaultdict(list)

    def check_and_record(self, workspace_id: int) -> bool:
        now = time.monotonic()
        window_start = now - RATE_LIMIT_WINDOW_SECONDS
        calls = self._calls[workspace_id]
        
        # Prune old timestamps
        while calls and calls[0] < window_start:
            calls.pop(0)

        if len(calls) >= RATE_LIMIT_CALLS:
            return False

        calls.append(now)
        return True

    def reset(self, workspace_id: Optional[int] = None) -> None:
        if workspace_id is None:
            self._calls.clear()
        else:
            self._calls.pop(workspace_id, None)


rate_limiter = WorkspaceRateLimiter()


def build_workspace_caller(workspace_id: int, execution_context: dict[str, Any]):
    """Construct an async call(model, prompt) function for route_request.
    Routes simulated models to call_model and live models to providers.complete.
    Records actual token usage and updates slot health.
    """
    async def call(model: dict[str, Any], prompt: str) -> str:
        exec_mode = model.get("execution_mode", "simulated")
        provider = model.get("provider", "simulated")

        if exec_mode != "live" or provider == "simulated":
            # Deterministic simulation
            return await call_model(model, prompt)

        # Safety checks for live execution
        if len(prompt) > MAX_PROMPT_CHARS:
            raise ProviderError(
                status_code=400,
                error_type="prompt_too_long",
                message=f"Prompt exceeds maximum length of {MAX_PROMPT_CHARS} characters.",
            )

        if not rate_limiter.check_and_record(workspace_id):
            raise ProviderError(
                status_code=429,
                error_type="rate_limit_exceeded",
                message=f"Workspace live call rate limit exceeded ({RATE_LIMIT_CALLS} calls/minute).",
            )

        conn_data = get_slot_connection_data(workspace_id, model["name"])
        if not conn_data:
            raise ProviderError(
                status_code=500,
                error_type="config_error",
                message=f"Model configuration not found for '{model['name']}'.",
            )

        slot_number = conn_data["slot_number"]
        provider_name = conn_data["provider"]
        model_id = conn_data["model_id"]
        api_key = conn_data["api_key"]
        max_tokens = conn_data.get("max_output_tokens") or 1024
        cost_in = conn_data.get("cost_per_1k_input")
        cost_out = conn_data.get("cost_per_1k_output")

        try:
            result = await complete(
                provider=provider_name,
                model_id=model_id,
                api_key=api_key,
                prompt=prompt,
                max_tokens=max_tokens,
                cost_per_1k_input=cost_in,
                cost_per_1k_output=cost_out,
            )

            # Update live metrics on success
            update_measured_latency(workspace_id, slot_number, result.latency_ms)
            update_slot_status(workspace_id, slot_number, "up", None)

            # Compute actual cost in project currency (rupees)
            cost_in = conn_data.get("cost_per_1k_input")
            cost_out = conn_data.get("cost_per_1k_output")
            if cost_in is not None and cost_out is not None:
                actual_cost = (result.input_tokens / 1000.0) * cost_in + (result.output_tokens / 1000.0) * cost_out
            elif cost_in is not None:
                actual_cost = (result.input_tokens / 1000.0) * cost_in
            elif cost_out is not None:
                actual_cost = (result.output_tokens / 1000.0) * cost_out
            else:
                fallback_rate = model.get("cost_per_1k", 0.0)
                actual_cost = ((result.input_tokens + result.output_tokens) / 1000.0) * fallback_rate

            # Store exact 4-key usage metrics in execution_context
            execution_context["usage"] = {
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "actual_cost": actual_cost,
                "latency_ms": result.latency_ms,
            }
            execution_context["actual_cost"] = actual_cost
            return result.content

        except ProviderError as pe:
            # Only a provider auth error (401/403) may change a slot's status (to 'down', status_reason 'invalid API key')
            # 429, 5xx, timeout and network errors must raise for failover and leave status unchanged.
            if pe.status_code in (401, 403):
                update_slot_status(workspace_id, slot_number, "down", "invalid API key")
            raise

    return call
