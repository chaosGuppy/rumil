"""Tests for rumil.pricing.compute_cost."""

import json
from pathlib import Path

import pytest

from rumil.pricing import _load_pricing, compute_cost

PRICING_JSON_PATH = Path(__file__).parent.parent / "src" / "rumil" / "pricing.json"
PRICING_MODELS = tuple(json.loads(PRICING_JSON_PATH.read_text()).keys())


@pytest.mark.parametrize("model", PRICING_MODELS)
def test_compute_cost_matches_pricing_json_rates(model):
    rates = _load_pricing()[model]
    cost = compute_cost(
        model,
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_creation_input_tokens=1_000_000,
        cache_read_input_tokens=1_000_000,
    )
    expected = (
        rates["input_tokens_per_mtok"]
        + rates["output_tokens_per_mtok"]
        + rates["cache_creation_input_tokens_per_mtok"]
        + rates["cache_read_input_tokens_per_mtok"]
    )
    assert cost == pytest.approx(expected)


def test_compute_cost_zero_tokens_is_zero():
    for model in PRICING_MODELS:
        assert compute_cost(model, input_tokens=0, output_tokens=0) == 0.0


def test_compute_cost_startswith_fallback_for_dated_sonnet_variant():
    base_cost = compute_cost("claude-sonnet-4-6", input_tokens=1_000_000, output_tokens=0)
    dated_cost = compute_cost("claude-sonnet-4-6-20260101", input_tokens=1_000_000, output_tokens=0)
    assert dated_cost == base_cost
    assert base_cost > 0


def test_compute_cost_prefers_exact_match_over_prefix():
    cost = compute_cost("claude-haiku-4-5-20251001", input_tokens=1_000_000, output_tokens=0)
    haiku_rates = _load_pricing()["claude-haiku-4-5-20251001"]
    assert cost == pytest.approx(haiku_rates["input_tokens_per_mtok"] / 1.0)


def test_compute_cost_unknown_model_raises():
    with pytest.raises(KeyError, match="gpt-4"):
        compute_cost("gpt-4", input_tokens=100, output_tokens=100)


def test_compute_cost_unrelated_prefix_still_raises():
    with pytest.raises(KeyError):
        compute_cost("claude-opus", input_tokens=100, output_tokens=100)
