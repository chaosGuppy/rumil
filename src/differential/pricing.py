"""Compute USD cost for an LLM exchange based on token usage."""

import functools
import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)


@functools.cache
def _load_pricing() -> dict:
    path = Path(__file__).parent / "pricing.json"
    with open(path) as f:
        return json.load(f)


def compute_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
) -> float:
    """Return the USD cost for the given token usage.

    Unknown models log a warning and return 0.0.
    """
    pricing = _load_pricing()
    rates = pricing.get(model)
    if rates is None:
        log.warning("No pricing data for model %s", model)
        return 0.0
    return (
        input_tokens * rates["input_tokens_per_mtok"]
        + output_tokens * rates["output_tokens_per_mtok"]
        + cache_creation_input_tokens * rates["cache_creation_input_tokens_per_mtok"]
        + cache_read_input_tokens * rates["cache_read_input_tokens_per_mtok"]
    ) / 1_000_000
