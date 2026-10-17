"""Encryption and secret storage for provider API keys using Fernet (symmetric encryption).
Keys are encrypted at rest using NEUROROUTE_SECRET_KEY environment variable.
"""
import os
from typing import Optional
from cryptography.fernet import Fernet, InvalidToken

ENV_SECRET_KEY = "NEUROROUTE_SECRET_KEY"


def is_configured() -> bool:
    """Return True if NEUROROUTE_SECRET_KEY is set and is a valid Fernet key."""
    key = os.environ.get(ENV_SECRET_KEY, "").strip()
    if not key:
        return False
    try:
        Fernet(key.encode("utf-8"))
        return True
    except Exception:
        return False


class SecretConfigError(Exception):
    """Raised when secret key configuration is invalid or missing in production."""
    pass


def validate_production_secret() -> None:
    """Validate that NEUROROUTE_SECRET_KEY is configured and valid in production environments.

    Rules:
    1. If not running in production (is_production_environment() is False):
       Validation passes (preserves local SQLite development and non-prod tests).
    2. In production environment (VERCEL, ENVIRONMENT=production, ENV=production, NODE_ENV=production):
       - If NEUROROUTE_SECRET_KEY is missing or empty:
         raises SecretConfigError("NEUROROUTE_SECRET_KEY must be configured in production environment.")
       - If NEUROROUTE_SECRET_KEY is present but malformed / invalid Fernet key:
         raises SecretConfigError("NEUROROUTE_SECRET_KEY is malformed: must be a 32-byte url-safe base64-encoded key.")
    3. The error message NEVER includes the key's value.
    4. Does not silently generate a replacement key.
    """
    from src.gateway.db import is_production_environment

    if not is_production_environment():
        return

    key = os.environ.get(ENV_SECRET_KEY, "").strip()
    if not key:
        raise SecretConfigError(
            "NEUROROUTE_SECRET_KEY must be configured in production environment."
        )
    try:
        Fernet(key.encode("utf-8"))
    except Exception:
        raise SecretConfigError(
            "NEUROROUTE_SECRET_KEY is malformed: must be a 32-byte url-safe base64-encoded key."
        ) from None


def _get_fernet() -> Fernet:
    """Instantiate Fernet cipher from environment variable."""
    key = os.environ.get(ENV_SECRET_KEY, "").strip()
    if not key:
        raise ValueError("NEUROROUTE_SECRET_KEY is not set.")
    try:
        return Fernet(key.encode("utf-8"))
    except Exception:
        raise ValueError("NEUROROUTE_SECRET_KEY is invalid.") from None


def encrypt(plaintext: str) -> str:
    """Encrypt a plaintext string and return base64-encoded ciphertext."""
    if not plaintext:
        return ""
    fernet = _get_fernet()
    token = fernet.encrypt(plaintext.encode("utf-8"))
    return token.decode("utf-8")


def decrypt(ciphertext: str) -> str:
    """Decrypt base64-encoded ciphertext and return plaintext string."""
    if not ciphertext:
        return ""
    fernet = _get_fernet()
    try:
        decrypted = fernet.decrypt(ciphertext.encode("utf-8"))
        return decrypted.decode("utf-8")
    except InvalidToken as e:
        raise ValueError("Failed to decrypt API key: invalid token or secret key mismatch.") from e


def last4(key: str) -> Optional[str]:
    """Return the last 4 characters of an API key for safe display."""
    if not key:
        return None
    cleaned = key.strip()
    return cleaned[-4:] if len(cleaned) >= 4 else cleaned


# Aliases for readability
encrypt_secret = encrypt
decrypt_secret = decrypt
get_last4 = last4

