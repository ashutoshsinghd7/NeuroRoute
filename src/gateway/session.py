"""Guest session management for NeuroRoute workspaces.
Provides secure, cookie-based workspace isolation per browser session.
Guest sessions are scoped per browser and do not provide cross-device synchronization.
The raw session token is NEVER stored in the database or logged; only its SHA-256 hash is persisted.
"""
import hashlib
import secrets
from typing import Optional
from fastapi import Request, Response

from src.gateway.workspace_repo import (
    create_workspace,
    get_workspace_by_token_hash,
)

COOKIE_NAME = "nr_session"
SESSION_MAX_AGE_SECONDS = 30 * 24 * 3600  # 30 days


def hash_token(raw_token: str) -> str:
    """Return hex-encoded SHA-256 digest of the raw session token."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def is_request_secure(request: Request) -> bool:
    """Detect if connection is HTTPS (direct or behind an SSL-terminating reverse proxy)."""
    if request.url.scheme == "https":
        return True
    forwarded_proto = request.headers.get("x-forwarded-proto", "").lower()
    return forwarded_proto == "https"


async def get_workspace_id(request: Request, response: Response) -> int:
    """FastAPI dependency to resolve or create the client's isolated workspace.
    
    Resolution order:
    1. Read 'nr_session' cookie.
    2. If present and token hash matches an active, non-expired workspace, return that workspace ID.
    3. If missing, expired, or invalid token: create a fresh workspace with 6 default models,
       issue a new session token, set the HttpOnly cookie on the response, and return the new ID.
       
    SECURITY:
    - workspace_id is strictly server-resolved. It is NEVER read from query, body, or headers.
    - Raw token is never written to DB; only the SHA-256 hash is stored.
    """
    raw_token = request.cookies.get(COOKIE_NAME)
    if raw_token:
        token_hash = hash_token(raw_token)
        workspace = get_workspace_by_token_hash(token_hash)
        if workspace:
            if workspace.get("_slid") and response is not None:
                secure_flag = is_request_secure(request)
                response.set_cookie(
                    key=COOKIE_NAME,
                    value=raw_token,
                    max_age=SESSION_MAX_AGE_SECONDS,
                    httponly=True,
                    samesite="lax",
                    path="/",
                    secure=secure_flag,
                )
            return workspace["id"]

    # Issue a new guest session
    new_raw_token = secrets.token_urlsafe(32)
    new_token_hash = hash_token(new_raw_token)
    workspace_id = create_workspace(new_token_hash, ttl_days=30)

    secure_flag = is_request_secure(request)
    response.set_cookie(
        key=COOKIE_NAME,
        value=new_raw_token,
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        path="/",
        secure=secure_flag,
    )

    return workspace_id


async def get_optional_workspace(request: Request, response: Response) -> Optional[int]:
    """FastAPI dependency for optional workspace resolution.
    
    - No 'nr_session' cookie at all -> returns None (legacy mode).
    - Cookie present and valid -> returns that workspace_id (workspace mode).
    - Cookie present but unknown or expired -> creates a NEW workspace, seeds defaults,
      sets the new session cookie on response, and returns the new workspace_id (workspace mode).
    """
    raw_token = request.cookies.get(COOKIE_NAME)
    if not raw_token:
        return None

    token_hash = hash_token(raw_token)
    workspace = get_workspace_by_token_hash(token_hash)
    if workspace:
        if workspace.get("_slid") and response is not None:
            secure_flag = is_request_secure(request)
            response.set_cookie(
                key=COOKIE_NAME,
                value=raw_token,
                max_age=SESSION_MAX_AGE_SECONDS,
                httponly=True,
                samesite="lax",
                path="/",
                secure=secure_flag,
            )
        return workspace["id"]

    # Cookie was provided, but token is unknown or expired -> create new workspace
    new_raw_token = secrets.token_urlsafe(32)
    new_token_hash = hash_token(new_raw_token)
    workspace_id = create_workspace(new_token_hash, ttl_days=30)

    secure_flag = is_request_secure(request)
    response.set_cookie(
        key=COOKIE_NAME,
        value=new_raw_token,
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        path="/",
        secure=secure_flag,
    )

    return workspace_id
