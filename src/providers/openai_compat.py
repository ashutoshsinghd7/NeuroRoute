"""Adapter for OpenAI-compatible chat completion APIs (OpenAI, Groq, DeepSeek, OpenRouter, Ollama)."""
from typing import Optional
import httpx

from src.providers.registry import ProviderError, ProviderResult


async def complete_openai(
    base_url: str,
    model_id: str,
    api_key: Optional[str],
    prompt: str,
    max_tokens: int,
    client: httpx.AsyncClient,
) -> ProviderResult:
    """Execute a completion against an OpenAI-compatible endpoint."""
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    payload = {
        "model": model_id,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
    }

    try:
        response = await client.post(url, headers=headers, json=payload, timeout=30.0)
    except httpx.TimeoutException as e:
        raise ProviderError("timeout", "Provider request timed out.") from e
    except httpx.RequestError as e:
        raise ProviderError("network", "Network error connecting to provider.") from e

    status = response.status_code
    if status in (401, 403):
        raise ProviderError("auth", "Authentication failed with provider.")
    elif status == 429:
        raise ProviderError("rate_limit", "Provider rate limit reached.")
    elif status >= 500:
        raise ProviderError("server", "Provider returned a server error.")
    elif status in (400, 404, 422):
        raise ProviderError("bad_request", "Invalid request parameters sent to provider.")
    elif status != 200:
        raise ProviderError("server", f"Unexpected status code {status} from provider.")

    try:
        data = response.json()
        text = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})
        input_tokens = int(usage.get("prompt_tokens", len(prompt) // 4 or 1))
        output_tokens = int(usage.get("completion_tokens", len(text) // 4 or 1))
        return ProviderResult(text=text, input_tokens=input_tokens, output_tokens=output_tokens)
    except Exception as e:
        raise ProviderError("server", "Failed to parse provider response.") from e


complete_openai_compat = complete_openai

