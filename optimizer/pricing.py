from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .domain import RoundTimeoutError


@dataclass(frozen=True)
class ProbeModelSelection:
    model: str | None
    estimated_cost_usd: float | None


def load_model_pricing(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None or not path.is_file():
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except RoundTimeoutError:
        raise
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    return {
        str(model): details
        for model, details in value.items()
        if isinstance(details, dict)
    }


def _estimated_cost(
    details: dict[str, Any], *, input_tokens: int, output_tokens: int
) -> float | None:
    input_cost = details.get("input_cost_per_token")
    output_cost = details.get("output_cost_per_token")
    if not isinstance(input_cost, (int, float)) or not isinstance(
        output_cost, (int, float)
    ):
        return None
    return max(0, input_tokens) * float(input_cost) + max(0, output_tokens) * float(
        output_cost
    )


def select_probe_model(
    candidates: set[str],
    allowed_models: tuple[str, ...],
    pricing: dict[str, dict[str, Any]],
    *,
    input_tokens: int,
    output_tokens: int,
) -> ProbeModelSelection:
    eligible = [model for model in allowed_models if model in candidates]
    if not eligible:
        return ProbeModelSelection(None, None)

    priced: list[tuple[float, int, str]] = []
    for order, model in enumerate(eligible):
        cost = _estimated_cost(
            pricing.get(model, {}),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        if cost is None:
            return ProbeModelSelection(None, None)
        priced.append((cost, order, model))
    cost, _order, model = min(priced)
    return ProbeModelSelection(model, cost)
