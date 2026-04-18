"""Compute USD cost for an LLM exchange based on token usage."""

import functools
import json
from pathlib import Path


@functools.cache
def _load_pricing() -> dict:
    path = Path(__file__).parent / "pricing.json"
    with open(path) as f:
        return json.load(f)


def _resolve_rates(model: str, pricing: dict) -> dict:
    """Return the rate entry for `model`.

    Exact match is preferred. Falls back to a `startswith` match so that dated
    variants like `claude-sonnet-4-6-20260101` resolve to the base
    `claude-sonnet-4-6` entry. Raises `KeyError` if no entry matches.
    """
    if model in pricing:
        return pricing[model]
    prefix_matches = [key for key in pricing if model.startswith(key)]
    if prefix_matches:
        # Prefer the longest matching prefix to avoid shadowing more specific entries.
        best = max(prefix_matches, key=len)
        return pricing[best]
    raise KeyError(f"No pricing data for model {model!r}")


def compute_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
) -> float:
    """Return the USD cost for the given token usage.

    Raises `KeyError` if the model has no pricing entry (exact or prefix match).
    """
    pricing = _load_pricing()
    rates = _resolve_rates(model, pricing)
    return (
        input_tokens * rates["input_tokens_per_mtok"]
        + output_tokens * rates["output_tokens_per_mtok"]
        + cache_creation_input_tokens * rates["cache_creation_input_tokens_per_mtok"]
        + cache_read_input_tokens * rates["cache_read_input_tokens_per_mtok"]
    ) / 1_000_000
