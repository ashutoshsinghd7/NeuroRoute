"""Provider allowlist, error definitions, and registry.
SSRF protection: only allowlisted providers and hardcoded base URLs are permitted.
"""
from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    base_url: str
    requires_key: bool
    adapter: str


PROVIDER_ALLOWLIST: dict[str, ProviderConfig] = {
    "openai": ProviderConfig(
        name="openai",
        base_url="https://api.openai.com/v1",
        requires_key=True,
        adapter="openai_compat",
    ),
    "groq": ProviderConfig(
        name="groq",
        base_url="https://api.groq.com/openai/v1",
        requires_key=True,
        adapter="openai_compat",
    ),
    "deepseek": ProviderConfig(
        name="deepseek",
        base_url="https://api.deepseek.com/v1",
        requires_key=True,
        adapter="openai_compat",
    ),
    "openrouter": ProviderConfig(
        name="openrouter",
        base_url="https://openrouter.ai/api/v1",
        requires_key=True,
        adapter="openai_compat",
    ),
    "anthropic": ProviderConfig(
        name="anthropic",
        base_url="https://api.anthropic.com/v1",
        requires_key=True,
        adapter="anthropic",
    ),
    "ollama": ProviderConfig(
        name="ollama",
        base_url="http://localhost:11434/v1",
        requires_key=False,
        adapter="openai_compat",
    ),
}


@dataclass
class ProviderResult:
    text: str
    input_tokens: int
    output_tokens: int
    latency_ms: int = 0
    cost_usd: float = 0.0

    @property
    def content(self) -> str:
        return self.text

    @property
    def prompt_tokens(self) -> int:
        return self.input_tokens

    @property
    def completion_tokens(self) -> int:
        return self.output_tokens

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class ProviderError(Exception):
    """Normalized provider error.
    Kinds: 'auth', 'rate_limit', 'server', 'bad_request', 'timeout', 'network'.
    Messages are strictly sanitized and never contain keys, headers, or request bodies.
    """
    def __init__(self, *args, **kwargs):
        if len(args) == 3 and isinstance(args[0], int):
            status_code, kind, message = args
        elif len(args) == 2:
            if isinstance(args[0], int):
                status_code, message = args
                kind = "error"
            else:
                kind, message = args
                status_code = kwargs.get("status_code")
        elif len(args) == 1:
            kind = "error"
            message = args[0]
            status_code = kwargs.get("status_code", 500)
        else:
            kind = kwargs.get("kind", kwargs.get("error_type", "error"))
            message = kwargs.get("message", "Provider error")
            status_code = kwargs.get("status_code")

        code_map = {
            "auth": 401,
            "auth_error": 401,
            "rate_limit": 429,
            "rate_limit_exceeded": 429,
            "server": 500,
            "server_error": 500,
            "bad_request": 400,
            "prompt_too_long": 400,
            "invalid_provider": 400,
            "timeout": 504,
            "network": 502,
            "config_error": 500,
        }
        if status_code is None:
            status_code = code_map.get(kind, 500)

        super().__init__(message)
        self.kind = kind
        self.message = message
        self.status_code = status_code

    @property
    def error_type(self) -> str:
        return self.kind


def get_provider_config(provider_name: str) -> Optional[ProviderConfig]:
    """Retrieve provider configuration from allowlist or None if invalid."""
    return PROVIDER_ALLOWLIST.get(provider_name.lower())


def is_provider_allowed(provider_name: str) -> bool:
    """Return True if provider is in the allowlist."""
    return provider_name.lower() in PROVIDER_ALLOWLIST


def list_allowed_providers() -> list[dict[str, Any]]:
    """Return public metadata of allowed providers (without exposing internal URLs)."""
    return [
        {"provider": cfg.name, "requires_key": cfg.requires_key}
        for cfg in PROVIDER_ALLOWLIST.values()
    ]
