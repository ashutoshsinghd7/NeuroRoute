"""API endpoints for workspace model slot management.
All endpoints are scoped per-workspace via guest sessions (nr_session cookie).
Prefix: /api/workspace
"""
import os
import random
from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse

from src.gateway.schemas import ModelUpdate, SlotModelIn
from src.gateway.secrets_store import is_configured
from src.gateway.session import get_workspace_id
from src.gateway.workspace_repo import (
    DuplicateModelNameError,
    InvalidSlotError,
    SlotEmptyError,
    SlotOccupiedError,
    add_model,
    get_slot,
    get_slot_connection_data,
    list_slots,
    load_demo_models,
    patch_model,
    record_test_run,
    remove_model,
    replace_model,
    toggle_enabled,
    update_measured_latency,
    update_slot_status,
)
from src.providers import complete
from src.providers.registry import ProviderError, is_provider_allowed, list_allowed_providers

router = APIRouter(prefix="/api/workspace", tags=["workspace"])


def error_response(status_code: int, code: str, message: str) -> JSONResponse:
    """Format all error responses using the uniform error contract."""
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
    )


def validate_slot_number(slot: int) -> bool:
    """Validate slot number is between 1 and 6."""
    return 1 <= slot <= 6


@router.get("/providers")
async def get_providers():
    """Return the list of allowed live providers."""
    return {"providers": list_allowed_providers()}


@router.post("/load-demo-models")
async def load_demo_workspace_models(workspace_id: int = Depends(get_workspace_id)):
    """Fill only empty slots with simulated registry models, leaving occupied slots untouched."""
    slots = load_demo_models(workspace_id)
    return {"slots": slots}


@router.get("/models")
async def get_workspace_models(workspace_id: int = Depends(get_workspace_id)):
    """Retrieve all 6 slots for the active workspace."""
    slots = list_slots(workspace_id)
    return {"slots": slots}


@router.post("/models/{slot}", status_code=status.HTTP_201_CREATED)
async def add_workspace_model(
    slot: int,
    payload: SlotModelIn,
    workspace_id: int = Depends(get_workspace_id),
):
    """Add a model to an empty slot in the workspace."""
    if not validate_slot_number(slot):
        return error_response(400, "INVALID_SLOT", f"Slot number must be between 1 and 6, got {slot}")

    # Validate live mode requirements
    if payload.execution_mode == "live":
        if not is_provider_allowed(payload.provider):
            return error_response(400, "INVALID_PROVIDER", f"Provider '{payload.provider}' is not supported for live execution.")

        if payload.provider != "ollama":
            if not is_configured():
                return error_response(503, "SECRET_KEY_MISSING", "Live mode needs NEUROROUTE_SECRET_KEY. See README.")

            api_key_str = payload.api_key.get_secret_value().strip() if payload.api_key else ""
            if not api_key_str:
                return error_response(400, "API_KEY_REQUIRED", f"API key is required for provider '{payload.provider}'.")

    try:
        model = add_model(workspace_id, slot, payload.model_dump())
        return model
    except SlotOccupiedError as e:
        return error_response(409, "SLOT_OCCUPIED", str(e))
    except DuplicateModelNameError as e:
        return error_response(409, "DUPLICATE_MODEL_NAME", str(e))
    except InvalidSlotError as e:
        return error_response(400, "INVALID_SLOT", str(e))


@router.put("/models/{slot}")
async def replace_workspace_model(
    slot: int,
    payload: SlotModelIn,
    workspace_id: int = Depends(get_workspace_id),
):
    """Fully replace an occupied slot with a new model specification."""
    if not validate_slot_number(slot):
        return error_response(400, "INVALID_SLOT", f"Slot number must be between 1 and 6, got {slot}")

    # Check slot occupancy
    existing_model = get_slot(workspace_id, slot)
    if not existing_model:
        return error_response(404, "SLOT_EMPTY", f"Slot {slot} is empty; cannot replace.")

    # Validate live mode requirements
    if payload.execution_mode == "live":
        if not is_provider_allowed(payload.provider):
            return error_response(400, "INVALID_PROVIDER", f"Provider '{payload.provider}' is not supported for live execution.")

        has_new_key = bool(payload.api_key and payload.api_key.get_secret_value().strip())
        provider_unchanged = (payload.provider == existing_model.get("provider"))
        existing_has_key = existing_model.get("has_key", False)

        if payload.provider != "ollama":
            if not has_new_key and not (provider_unchanged and existing_has_key):
                return error_response(400, "API_KEY_REQUIRED", f"API key is required for provider '{payload.provider}'.")
            if has_new_key and not is_configured():
                return error_response(503, "SECRET_KEY_MISSING", "Live mode needs NEUROROUTE_SECRET_KEY. See README.")

    try:
        model = replace_model(workspace_id, slot, payload.model_dump())
        return model
    except SlotEmptyError as e:
        return error_response(404, "SLOT_EMPTY", str(e))
    except DuplicateModelNameError as e:
        return error_response(409, "DUPLICATE_MODEL_NAME", str(e))
    except InvalidSlotError as e:
        return error_response(400, "INVALID_SLOT", str(e))


@router.patch("/models/{slot}")
async def patch_workspace_model(
    slot: int,
    payload: ModelUpdate,
    workspace_id: int = Depends(get_workspace_id),
):
    """Partially update status, latency_ms, or failure_rate for a model."""
    if not validate_slot_number(slot):
        return error_response(400, "INVALID_SLOT", f"Slot number must be between 1 and 6, got {slot}")

    try:
        model = patch_model(workspace_id, slot, payload.model_dump(exclude_none=True))
        return model
    except SlotEmptyError as e:
        return error_response(404, "SLOT_EMPTY", str(e))
    except InvalidSlotError as e:
        return error_response(400, "INVALID_SLOT", str(e))


@router.delete("/models/{slot}")
async def remove_workspace_model(
    slot: int,
    workspace_id: int = Depends(get_workspace_id),
):
    """Remove a model from a slot, making the slot empty."""
    if not validate_slot_number(slot):
        return error_response(400, "INVALID_SLOT", f"Slot number must be between 1 and 6, got {slot}")

    try:
        return remove_model(workspace_id, slot)
    except SlotEmptyError as e:
        return error_response(404, "SLOT_EMPTY", str(e))
    except InvalidSlotError as e:
        return error_response(400, "INVALID_SLOT", str(e))


@router.patch("/models/{slot}/toggle")
async def toggle_workspace_model_enabled(
    slot: int,
    workspace_id: int = Depends(get_workspace_id),
):
    """Toggle the enabled status of a model in the slot."""
    if not validate_slot_number(slot):
        return error_response(400, "INVALID_SLOT", f"Slot number must be between 1 and 6, got {slot}")

    try:
        return toggle_enabled(workspace_id, slot)
    except SlotEmptyError as e:
        return error_response(404, "SLOT_EMPTY", str(e))
    except InvalidSlotError as e:
        return error_response(400, "INVALID_SLOT", str(e))


@router.post("/models/{slot}/test")
async def test_workspace_model(
    slot: int,
    workspace_id: int = Depends(get_workspace_id),
):
    """Test the model in the given slot.
    - If execution_mode == 'simulated': deterministic simulation.
    - If execution_mode == 'live': 1-token live ping test.
    """
    if not validate_slot_number(slot):
        return error_response(400, "INVALID_SLOT", f"Slot number must be between 1 and 6, got {slot}")

    model = get_slot(workspace_id, slot)
    if not model:
        return error_response(404, "SLOT_EMPTY", f"Slot {slot} is empty; cannot run test.")

    if model.get("execution_mode") == "live":
        conn_data = get_slot_connection_data(workspace_id, model["name"])
        if not conn_data:
            return error_response(500, "CONFIG_ERROR", f"Configuration not found for '{model['name']}'.")

        try:
            result = await complete(
                provider=conn_data["provider"],
                model_id=conn_data["model_id"],
                api_key=conn_data["api_key"],
                prompt="ping",
                max_tokens=1,
                cost_per_1k_input=conn_data.get("cost_per_1k_input"),
                cost_per_1k_output=conn_data.get("cost_per_1k_output"),
            )
            update_measured_latency(workspace_id, slot, result.latency_ms)
            update_slot_status(workspace_id, slot, "up", None)
            record_test_run(workspace_id, slot, True, result.latency_ms, f"Live ping succeeded for '{model['name']}'")
            return {
                "success": True,
                "latency_ms": result.latency_ms,
                "message": f"Live ping succeeded for '{model['name']}'",
            }
        except ProviderError as pe:
            if pe.status_code in (401, 403):
                update_slot_status(workspace_id, slot, "down", "invalid API key")
            record_test_run(workspace_id, slot, False, 0, pe.message)
            return {
                "success": False,
                "latency_ms": 0,
                "message": pe.message,
            }

    # Simulated mode
    seed_value = os.environ.get("NEUROROUTE_SEED", "") + model["name"]
    rng = random.Random(seed_value)

    if model["status"] == "down":
        success = False
        message = f"Model '{model['name']}' is down"
    else:
        roll = rng.random()
        success = roll >= model["failure_rate"]
        message = (
            f"Simulated test passed for '{model['name']}'"
            if success
            else f"Simulated test failed for '{model['name']}' (failure rate {model['failure_rate']})"
        )

    latency_ms = model["latency_ms"]
    record_test_run(workspace_id, slot, success, latency_ms, message)

    return {
        "success": success,
        "latency_ms": latency_ms,
        "message": message,
    }
