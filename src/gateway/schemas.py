"""Data shapes used by the whole gateway.

CP0 only needs the model shape and the registry loader.
Request / response shapes are added in CP1.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

# The one file that describes every model. Judges can edit it.
REGISTRY_PATH = Path(__file__).resolve().parents[1] / "models" / "registry.json"


class ModelSpec(BaseModel):
    """One model in our fictional catalogue."""

    name: str = Field(min_length=1)
    capability: int = Field(ge=1, le=5)        # 1 = simple ... 5 = very smart
    context_limit: int = Field(gt=0)           # max tokens it can read
    cost_per_1k: float = Field(ge=0)           # rupees per 1,000 tokens
    latency_ms: int = Field(gt=0)              # typical answer time
    failure_rate: float = Field(ge=0, le=1)    # 0.05 = fails 5% of calls
    status: Literal["up", "down"] = "up"


def load_registry(path: Path = REGISTRY_PATH) -> list[ModelSpec]:
    """Read registry.json, check every model, and return the list.

    Raises ValueError if the file is empty or two models share a name.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    models = [ModelSpec(**item) for item in raw]  # pydantic checks each field

    if not models:
        raise ValueError("registry is empty")
    names = [m.name for m in models]
    if len(names) != len(set(names)):
        raise ValueError("registry has duplicate model names")
    return models


def print_registry(models: list[ModelSpec]) -> None:
    """Print the catalogue as a neat table (used for the CP0 check)."""
    header = f"{'MODEL':<12}{'CAP':>4}{'CONTEXT':>10}{'Rs/1K':>8}{'LATENCY':>10}{'FAIL%':>7}  STATUS"
    print(header)
    print("-" * len(header))
    for m in models:
        print(
            f"{m.name:<12}{m.capability:>4}{m.context_limit:>10,}"
            f"{m.cost_per_1k:>8.2f}{m.latency_ms:>8} ms{m.failure_rate * 100:>6.0f}%  {m.status}"
        )
    print(f"\n{len(models)} models loaded, all valid.")



# ---------------------------------------------------------------------------
# CP1 — request shape
# ---------------------------------------------------------------------------

# The three values the user can pick for preference.
PreferenceType = Literal["cheapest", "fastest", "highest_quality"]


class RouteRequest(BaseModel):
    """The body of POST /route.

    All numbers must be positive. min_capability must be 1–5.
    preference must be one of: cheapest, fastest, highest_quality.
    required_context is optional — leave it out if you do not know.
    """

    prompt: str = Field(min_length=1, description="The text to send to the model.")
    max_latency_ms: int = Field(gt=0, description="Hard upper limit on answer time (ms).")
    max_cost: float = Field(gt=0, description="Hard upper limit on cost (Rs per call).")
    min_capability: int = Field(ge=1, le=5, description="Minimum smarts needed (1=easy, 5=hardest).")
    required_context: int | None = Field(
        default=None, gt=0,
        description="Minimum token window the model must support. Omit if unknown."
    )
    preference: PreferenceType = Field(
        description="What to optimise for: cheapest, fastest, or highest_quality."
    )


if __name__ == "__main__":
    print_registry(load_registry())
