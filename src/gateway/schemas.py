"""Input shapes (Pydantic). Bad input is rejected at the door (CP1)."""
from typing import Literal, Optional
from pydantic import BaseModel, Field, SecretStr, model_validator


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


class SlotModelIn(BaseModel):
    """Input payload to add or replace a model in a specific workspace slot."""
    model_config = {"extra": "forbid"}

    model_name: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    capability: int = Field(ge=1, le=5)
    context_limit: int = Field(gt=0)
    cost_per_1k: Optional[float] = Field(default=None, ge=0.0)
    latency_ms: int = Field(gt=0)
    failure_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    provider: str = Field(default="simulated")
    status: Literal["up", "down"] = "up"
    enabled: bool = True

    execution_mode: Literal["simulated", "live"] = "simulated"
    api_key: Optional[SecretStr] = None
    cost_per_1k_input: Optional[float] = Field(default=None, ge=0.0)
    cost_per_1k_output: Optional[float] = Field(default=None, ge=0.0)
    max_output_tokens: int = Field(default=512, ge=1, le=4096)

    @model_validator(mode="after")
    def compute_cost_per_1k(self) -> "SlotModelIn":
        if self.cost_per_1k_input is not None and self.cost_per_1k_output is not None:
            self.cost_per_1k = (self.cost_per_1k_input + self.cost_per_1k_output) / 2.0
        elif self.cost_per_1k is None:
            if self.cost_per_1k_input is not None:
                self.cost_per_1k = self.cost_per_1k_input
            elif self.cost_per_1k_output is not None:
                self.cost_per_1k = self.cost_per_1k_output
            else:
                self.cost_per_1k = 0.0
        return self


def load_registry(path: Optional[str] = None) -> list[dict]:
    """Load model registry from JSON file (default: src/models/registry.json)."""
    import json
    import os
    if path is None:
        src_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(src_dir, "models", "registry.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)
