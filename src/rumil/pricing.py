"""Compute USD cost for an LLM exchange based on token usage."""

import functools
import json
from pathlib import Path
from typing import Any


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


def usd_from_usage(model: str, usage: Any) -> float:
    """Return the USD cost for an Anthropic `Usage` object.

    Accepts any object with `input_tokens`, `output_tokens`, and the two
    optional cache counters; also accepts a dict-shaped payload with the
    same keys. Missing cache fields default to 0.
    """
    if isinstance(usage, dict):
        get = usage.get
        input_tokens = int(get("input_tokens") or 0)
        output_tokens = int(get("output_tokens") or 0)
        cache_creation = int(get("cache_creation_input_tokens") or 0)
        cache_read = int(get("cache_read_input_tokens") or 0)
    else:
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        cache_creation = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
    return compute_cost(
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=cache_creation,
        cache_read_input_tokens=cache_read,
    )
