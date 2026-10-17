"""Adapter for Anthropic Messages API."""
import httpx

from src.providers.registry import ProviderError, ProviderResult


async def complete_anthropic(
    base_url: str,
    model_id: str,
    api_key: str,
    prompt: str,
    max_tokens: int,
    client: httpx.AsyncClient,
) -> ProviderResult:
    """Execute a completion against the Anthropic Messages endpoint."""
    url = f"{base_url.rstrip('/')}/messages"
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }

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
        content_items = data.get("content", [])
        text = content_items[0]["text"] if content_items else ""
        usage = data.get("usage", {})
        input_tokens = int(usage.get("input_tokens", len(prompt) // 4 or 1))
        output_tokens = int(usage.get("output_tokens", len(text) // 4 or 1))
        return ProviderResult(text=text, input_tokens=input_tokens, output_tokens=output_tokens)
    except Exception as e:
        raise ProviderError("server", "Failed to parse provider response.") from e
