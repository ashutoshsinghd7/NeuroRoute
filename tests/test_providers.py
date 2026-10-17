"""Tests for provider adapters (OpenAI-compatible and Anthropic).
All external HTTP requests are mocked via httpx.MockTransport. ZERO real network calls.
"""
import httpx
import pytest

from src.providers import complete
from src.providers.anthropic import complete_anthropic
from src.providers.openai_compat import complete_openai
from src.providers.registry import ProviderError, get_provider_config, list_allowed_providers


def test_allowed_providers_list():
    """Verify provider allowlist contains supported providers."""
    providers = [p["provider"] for p in list_allowed_providers()]
    assert "openai" in providers
    assert "groq" in providers
    assert "deepseek" in providers
    assert "openrouter" in providers
    assert "anthropic" in providers
    assert "ollama" in providers


@pytest.mark.asyncio
async def test_openai_compat_success():
    """OpenAI compatible completion parses response, tokens, and calculates cost via complete()."""
    def handler(request: httpx.Request):
        assert request.headers["authorization"] == "Bearer test-api-key-1234"
        assert b'"model":"gpt-4o-mini"' in request.content
        assert b'"prompt test"' in request.content
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "Hello from mock OpenAI"}}],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 50,
                    "total_tokens": 150,
                },
            },
        )

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    result = await complete(
        provider="openai",
        model_id="gpt-4o-mini",
        api_key="test-api-key-1234",
        prompt="prompt test",
        max_tokens=100,
        cost_per_1k_input=0.01,
        cost_per_1k_output=0.03,
        client=mock_client,
    )

    assert result.content == "Hello from mock OpenAI"
    assert result.prompt_tokens == 100
    assert result.completion_tokens == 50
    assert result.total_tokens == 150
    # Cost: (100/1000)*0.01 + (50/1000)*0.03 = 0.001 + 0.0015 = 0.0025
    assert abs(result.cost_usd - 0.0025) < 1e-6


@pytest.mark.asyncio
async def test_openai_compat_auth_error_401():
    """401 from provider maps to ProviderError(401, 'auth')."""
    def handler(request: httpx.Request):
        return httpx.Response(
            401,
            json={"error": {"message": "Incorrect API key provided"}},
        )

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    with pytest.raises(ProviderError) as exc_info:
        await complete(
            provider="groq",
            model_id="llama-3.3-70b-versatile",
            api_key="bad-key",
            prompt="hi",
            client=mock_client,
        )
    assert exc_info.value.status_code == 401
    assert exc_info.value.error_type == "auth"


@pytest.mark.asyncio
async def test_openai_compat_rate_limit_429():
    """429 from provider maps to ProviderError(429, 'rate_limit')."""
    def handler(request: httpx.Request):
        return httpx.Response(
            429,
            json={"error": {"message": "Rate limit exceeded"}},
        )

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    with pytest.raises(ProviderError) as exc_info:
        await complete(
            provider="deepseek",
            model_id="deepseek-chat",
            api_key="valid-key",
            prompt="hi",
            client=mock_client,
        )
    assert exc_info.value.status_code == 429
    assert exc_info.value.error_type == "rate_limit"


@pytest.mark.asyncio
async def test_anthropic_success():
    """Anthropic adapter parses messages response and calculates cost via complete()."""
    def handler(request: httpx.Request):
        assert request.headers["x-api-key"] == "ant-test-key-9999"
        assert request.headers["anthropic-version"] == "2023-06-01"
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "Hello from mock Anthropic"}],
                "usage": {
                    "input_tokens": 200,
                    "output_tokens": 80,
                },
            },
        )

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    result = await complete(
        provider="anthropic",
        model_id="claude-3-5-sonnet-20241022",
        api_key="ant-test-key-9999",
        prompt="hello anthropic",
        max_tokens=256,
        cost_per_1k_input=0.003,
        cost_per_1k_output=0.015,
        client=mock_client,
    )

    assert result.content == "Hello from mock Anthropic"
    assert result.prompt_tokens == 200
    assert result.completion_tokens == 80
    assert result.total_tokens == 280
    # Cost: (200/1000)*0.003 + (80/1000)*0.015 = 0.0006 + 0.0012 = 0.0018
    assert abs(result.cost_usd - 0.0018) < 1e-6


@pytest.mark.asyncio
async def test_anthropic_server_error_500():
    """500 from Anthropic maps to ProviderError(500, 'server')."""
    def handler(request: httpx.Request):
        return httpx.Response(500, json={"error": {"message": "Overloaded"}})

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    with pytest.raises(ProviderError) as exc_info:
        await complete(
            provider="anthropic",
            model_id="claude-3-5-sonnet-20241022",
            api_key="ant-key",
            prompt="hi",
            client=mock_client,
        )
    assert exc_info.value.status_code == 500
    assert exc_info.value.error_type == "server"


@pytest.mark.asyncio
async def test_complete_router_unsupported_provider():
    """complete() rejects unsupported providers."""
    with pytest.raises(ProviderError) as exc_info:
        await complete(
            provider="evil_corp",
            model_id="evil-v1",
            api_key="key",
            prompt="hi",
        )
    assert exc_info.value.status_code == 400
