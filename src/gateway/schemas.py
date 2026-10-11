"""Input shapes (Pydantic). Bad input is rejected at the door (CP1)."""
from typing import Literal, Optional
from pydantic import BaseModel, Field


class RouteRequest(BaseModel):
    prompt: str = Field(min_length=1)
    max_latency_ms: int = Field(gt=0)            # answer must arrive within this time
    max_cost: float = Field(gt=0)                # must not cost more than this
    min_capability: int = Field(ge=1, le=5)      # model must be at least this smart
    required_context: int = Field(default=0, ge=0)  # tokens the model must be able to read
    preference: Literal["cheapest", "fastest", "highest_quality"] = "cheapest"


class ModelSpec(BaseModel):
    """Specification of a model in the fleet."""
    name: str
    capability: int = Field(ge=1, le=5)
    context_limit: int = Field(gt=0)
    cost_per_1k: float = Field(ge=0)
    latency_ms: int = Field(gt=0)
    failure_rate: float = Field(ge=0, le=1)
    status: Literal["up", "down"] = "up"


class ModelUpdate(BaseModel):
    """What the judge panel is allowed to change on a model."""
    status: Optional[Literal["up", "down"]] = None
    latency_ms: Optional[int] = Field(default=None, gt=0)
    failure_rate: Optional[float] = Field(default=None, ge=0, le=1)


def load_registry(path: Optional[str] = None) -> list[dict]:
    """Load model registry from JSON file (default: src/models/registry.json)."""
    import json
    import os
    if path is None:
        src_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(src_dir, "models", "registry.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)

