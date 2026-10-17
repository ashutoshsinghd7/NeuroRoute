"""FastAPI app: POST /route is the one door. Also serves the JavaScript dashboard at /."""
import copy
import json
import os
from contextlib import asynccontextmanager
from typing import Any, Optional
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from fastapi.exceptions import RequestValidationError

from src.gateway.db import close_pg_pool, get_backend, init_workspace_schema
from src.gateway.live_call import build_workspace_caller
from src.gateway.logger import get_history, init_db, log_request
from src.gateway.router import route_request
from src.gateway.schemas import ModelSpec, ModelUpdate, RouteRequest, load_registry
from src.gateway.session import get_optional_workspace, get_workspace_id
from src.gateway.workspace_api import router as workspace_router
from src.gateway.workspace_repo import (
    find_slot_by_model_name,
    load_routing_models,
    patch_model,
    reset_workspace,
)

SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from src.gateway.migrator import check_postgres_schema_readiness, init_postgres_schema

from src.gateway.secrets_store import validate_production_secret

# Read-only registry template for legacy /route calls (no cookie). Never mutated.
REGISTRY_MODELS = load_registry()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup validation:
    validate_production_secret()

    if get_backend() == "sqlite":
        init_db()
        init_workspace_schema()
    elif get_backend() == "postgres":
        if os.environ.get("AUTO_MIGRATE", "").lower() in ("1", "true", "yes"):
            init_postgres_schema()
        else:
            check_postgres_schema_readiness()
    yield
    # Shutdown:
    close_pg_pool()


app = FastAPI(title="NeuroRoute", lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Custom validation exception handler that redacts raw api_key values."""
    errors = copy.deepcopy(exc.errors())
    for error in errors:
        loc = error.get("loc", ())
        if "api_key" in loc:
            if "input" in error:
                error["input"] = "[REDACTED]"
    return JSONResponse(status_code=422, content={"detail": errors})


# CSRF guard for state-changing endpoints
@app.middleware("http")
async def csrf_and_workspace_guard(request: Request, call_next):
    is_guarded_path = (
        request.url.path.startswith("/api/workspace")
        or request.url.path == "/route"
        or request.url.path.startswith("/api/models")
        or request.url.path.startswith("/models")
    )
    if is_guarded_path and request.method in ("POST", "PUT", "PATCH", "DELETE"):
        # 1. Fetch Metadata check (Sec-Fetch-Site)
        sec_fetch_site = request.headers.get("sec-fetch-site")
        sfs_lower = sec_fetch_site.strip().lower() if sec_fetch_site else None
        if sfs_lower == "cross-site":
            return JSONResponse(
                status_code=403,
                content={"error": {"code": "CSRF_FAILED", "message": "Cross-site request rejected by Fetch Metadata"}},
            )

        # 2. Host determination & reverse proxy handling
        # Only trust X-Forwarded-Host if explicitly configured via TRUST_PROXY_HEADERS
        trust_proxies = os.environ.get("TRUST_PROXY_HEADERS", "").lower() in ("1", "true", "yes")
        host = request.headers.get("host", "").strip().lower()
        valid_hosts = set()
        if host:
            valid_hosts.add(host)
        if trust_proxies:
            forwarded_host = request.headers.get("x-forwarded-host")
            if forwarded_host:
                for h in forwarded_host.split(","):
                    h_clean = h.strip().lower()
                    if h_clean:
                        valid_hosts.add(h_clean)

        # 3. Origin validation
        origin = request.headers.get("origin")
        if origin:
            try:
                origin_netloc = urlparse(origin).netloc.lower()
            except Exception:
                origin_netloc = ""
            if not origin_netloc or not any(origin_netloc == vh for vh in valid_hosts):
                return JSONResponse(
                    status_code=403,
                    content={"error": {"code": "CSRF_FAILED", "message": "Origin header does not match Host"}},
                )

        # 4. Referer validation (when Origin is missing)
        referer = request.headers.get("referer")
        if not origin and referer:
            try:
                referer_netloc = urlparse(referer).netloc.lower()
            except Exception:
                referer_netloc = ""
            if not referer_netloc or not any(referer_netloc == vh for vh in valid_hosts):
                return JSONResponse(
                    status_code=403,
                    content={"error": {"code": "CSRF_FAILED", "message": "Referer header does not match Host"}},
                )

        # Determine if request has body
        content_length_header = request.headers.get("content-length")
        content_length = 0
        if content_length_header:
            try:
                content_length = int(content_length_header)
            except ValueError:
                content_length = 0

        has_body = (content_length > 0) or (request.headers.get("transfer-encoding") == "chunked")

        # 5. Cookie-authenticated body-less state-changing requests protection
        has_session_cookie = bool(request.cookies.get("nr_session"))
        if has_session_cookie and not has_body:
            if not origin and not referer:
                # If neither Origin nor Referer is present, verify acceptable Fetch Metadata
                if sfs_lower not in ("same-origin", "same-site"):
                    return JSONResponse(
                        status_code=403,
                        content={"error": {"code": "CSRF_FAILED", "message": "Missing origin verification on authenticated request"}},
                    )

        # 6. Content-Type check: require application/json ONLY when the request actually has a body
        if has_body:
            content_type = request.headers.get("content-type", "")
            if not content_type or "application/json" not in content_type.lower():
                return JSONResponse(
                    status_code=415,
                    content={"error": {"code": "UNSUPPORTED_MEDIA_TYPE", "message": "Content-Type must be application/json"}},
                )

    response = await call_next(request)
    return response


# Initialize database schemas for SQLite mode at import time (for tests and local dev)
if get_backend() == "sqlite":
    init_db()
    init_workspace_schema()

# Workspace API router
app.include_router(workspace_router)


@app.post("/route")
async def route(
    req: RouteRequest,
    workspace_id: Optional[int] = Depends(get_optional_workspace),
):
    if len(req.prompt) > 8000:
        return JSONResponse(
            status_code=400,
            content={"error": {"code": "PROMPT_TOO_LONG", "message": "Prompt exceeds maximum length of 8000 characters."}},
        )

    if workspace_id is None:
        # LEGACY MODE: No nr_session cookie provided (curl, automation scripts).
        # Route using a fresh, deep copy of the static registry. Never mutate the global template.
        models = copy.deepcopy(REGISTRY_MODELS)
        resp = await route_request(req, models)
        resp["trace"]["source"] = "legacy"
        log_request(req.model_dump(), resp, workspace_id=None)
        return resp
    else:
        # WORKSPACE MODE: Cookie present and valid, or newly issued for an unknown/expired token.
        # Route exclusively using this workspace's configured models.
        models = load_routing_models(workspace_id)
        if not models or not any(m.get("enabled", True) for m in models):
            trace = {
                "source": "workspace",
                "complexity": {"level": req.min_capability, "confidence": 1.0},
                "tokens": {"input": 0, "output": 0, "total": 0},
                "considered": [],
                "attempts": [],
                "cost": 0.0,
                "total_latency_ms": 0,
                "rejected": [],
            }
            resp = {
                "status": "NO_VALID_MODEL",
                "message": "No eligible model. Add a model to a slot or load demo models.",
                "answer": None,
                "chosen_model": None,
                "rejected": [],
                "trace": trace,
            }
            log_request(req.model_dump(), resp, workspace_id=workspace_id)
            return resp

        exec_context: dict[str, Any] = {}
        caller = build_workspace_caller(workspace_id, exec_context)
        resp = await route_request(req, models, call=caller)
        resp["trace"]["source"] = "workspace"
        if "usage" in exec_context:
            resp["trace"]["usage"] = exec_context["usage"]
            if "actual_cost" in exec_context and resp.get("status") == "OK":
                resp["trace"]["cost"] = exec_context["actual_cost"]
        log_request(req.model_dump(), resp, workspace_id=workspace_id)
        return resp


@app.get("/api/models")
@app.get("/models")
def list_models(workspace_id: int = Depends(get_workspace_id)):
    """Workspace-scoped legacy bridge: return occupied models as a flat list."""
    return load_routing_models(workspace_id)


@app.patch("/api/models/{name}")
@app.patch("/models/{name}")
def update_model(name: str, update: ModelUpdate, workspace_id: int = Depends(get_workspace_id)):
    """Workspace-scoped legacy bridge: update model in the caller's workspace."""
    slot = find_slot_by_model_name(workspace_id, name)
    if slot is None:
        raise HTTPException(404, f"unknown model {name}")
    return patch_model(workspace_id, slot, update.model_dump(exclude_none=True))


@app.post("/api/models/reset")
@app.post("/models/reset")
def reset_models(workspace_id: int = Depends(get_workspace_id)):
    """Workspace-scoped legacy bridge: reset fleet for the caller's workspace only."""
    return reset_workspace(workspace_id)


@app.get("/api/history")
@app.get("/history")
def history(limit: int = 50, workspace_id: Optional[int] = Depends(get_optional_workspace)):
    """Return history for the current workspace. No cookie -> return []."""
    return get_history(limit=limit, workspace_id=workspace_id)


# Keep this LAST: it serves src/dashboard/index.html at "/".
app.mount("/", StaticFiles(directory=os.path.join(SRC_DIR, "dashboard"), html=True), name="ui")
