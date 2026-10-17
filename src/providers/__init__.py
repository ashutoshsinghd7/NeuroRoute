"""Provider integration package providing unified completion across allowlisted providers."""
from typing import Optional
import httpx

from src.providers.anthropic import complete_anthropic
from src.providers.openai_compat import complete_openai
from src.providers.registry import (
    PROVIDER_ALLOWLIST,
    ProviderConfig,
    ProviderError,
    ProviderResult,
    get_provider_config,
    is_provider_allowed,
    list_allowed_providers,
)

__all__ = [
    "complete",
    "ProviderError",
    "ProviderResult",
    "ProviderConfig",
    "PROVIDER_ALLOWLIST",
    "get_provider_config",
    "is_provider_allowed",
    "list_allowed_providers",
]


import time

async def complete(
    provider: str,
    model_id: str,
    api_key: Optional[str],
    prompt: str,
    max_tokens: int = 512,
    cost_per_1k_input: Optional[float] = None,
    cost_per_1k_output: Optional[float] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> ProviderResult:
    """Execute a model completion using the appropriate provider adapter."""
    config = get_provider_config(provider)
    if not config:
        raise ProviderError("bad_request", f"Unsupported provider: '{provider}'.")

    if config.requires_key and not api_key:
        raise ProviderError("auth", f"Provider '{provider}' requires an API key.")

    owns_client = False
    if client is None:
        client = httpx.AsyncClient(follow_redirects=False, timeout=30.0)
        owns_client = True

    t0 = time.monotonic()
    try:
        if config.adapter == "anthropic":
            res = await complete_anthropic(
                base_url=config.base_url,
                model_id=model_id,
                api_key=api_key or "",
                prompt=prompt,
                max_tokens=max_tokens,
                client=client,
            )
        else:
            res = await complete_openai(
                base_url=config.base_url,
                model_id=model_id,
                api_key=api_key,
                prompt=prompt,
                max_tokens=max_tokens,
                client=client,
            )
        
        latency_ms = int((time.monotonic() - t0) * 1000)
        res.latency_ms = latency_ms
        cost_in = cost_per_1k_input if cost_per_1k_input is not None else 0.0
        cost_out = cost_per_1k_output if cost_per_1k_output is not None else 0.0
        res.cost_usd = (res.input_tokens / 1000.0) * cost_in + (res.output_tokens / 1000.0) * cost_out
        return res
    finally:
        if owns_client:
            await client.aclose()
