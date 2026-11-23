from __future__ import annotations

import json
import math
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
    except (OSError, ValueError):
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
    for rate in (input_cost, output_cost):
        if isinstance(rate, bool) or not isinstance(rate, (int, float)) or rate < 0:
            return None
    try:
        total = (
            max(0, input_tokens) * float(input_cost)
            + max(0, output_tokens) * float(output_cost)
        )
    except OverflowError:
        return None
    return total if math.isfinite(total) else None


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
            # One unpriced catalog entry must not disable probing for every
            # account; fall through to the cheapest priced candidate instead.
            continue
        priced.append((cost, order, model))
    if not priced:
        return ProbeModelSelection(None, None)
    cost, _order, model = min(priced)
    return ProbeModelSelection(model, cost)
