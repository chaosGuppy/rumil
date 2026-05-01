"""Tests for ``versus.judge.judge_config_is_current``.

The staleness detector returns False when any code-side input to the
judge has drifted from what a fresh run would produce — prompt shell
hash, code fingerprint (orch), or the recorded ``model_config``
snapshot vs. what the versus model registry currently says for that
model.

Tests construct judgment rows via ``make_judge_config`` using a
ModelConfig that matches (or deliberately differs from) what versus's
registry returns for the given model id. The staleness detector loads
versus/config.yaml from the repo root.
"""

from __future__ import annotations

import pathlib

import pytest
from versus.model_config import get_judge_model_config
from versus.versus_config import compute_judge_code_fingerprint, make_judge_config

from rumil.model_config import ModelConfig
from versus import config as versus_config
from versus import judge as versus_judge

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_VERSUS_CFG_PATH = _REPO_ROOT / "versus" / "config.yaml"


@pytest.fixture
def versus_cfg() -> versus_config.Config:
    return versus_config.load(_VERSUS_CFG_PATH)


def _make_blind_row(*, model: str, model_config: ModelConfig) -> dict:
    cfg, _, _ = make_judge_config(
        "blind",
        model=model,
        dimension="general_quality",
        model_config=model_config,
        prompt_hash=versus_judge.compute_judge_prompt_hash("general_quality", with_tools=False),
    )
    return {"judge_inputs": cfg}


def _make_orch_row(*, model: str, model_config: ModelConfig) -> dict:
    cfg, _, _ = make_judge_config(
        "orch",
        model=model,
        dimension="general_quality",
        model_config=model_config,
        prompt_hash=versus_judge.compute_judge_prompt_hash("general_quality", with_tools=True),
        tool_prompt_hash="11111111",
        pair_surface_hash="22222222",
        workspace_id="abcd1234",
        code_fingerprint=compute_judge_code_fingerprint(),
        workspace_state_hash="0011223344556677",
        budget=4,
        closer_hash="33333333",
    )
    return {"judge_inputs": cfg}


def test_blind_row_with_registry_config_is_current(versus_cfg):
    mc = get_judge_model_config("claude-haiku-4-5", cfg=versus_cfg)
    row = _make_blind_row(model="claude-haiku-4-5", model_config=mc)
    assert versus_judge.judge_config_is_current(row, "general_quality", cfg=versus_cfg) is True


def test_blind_row_with_drifted_temperature_is_stale(versus_cfg):
    mc = get_judge_model_config("claude-haiku-4-5", cfg=versus_cfg)
    drifted = ModelConfig(
        temperature=0.7,
        max_tokens=mc.max_tokens,
        top_p=mc.top_p,
        thinking=mc.thinking,
        effort=mc.effort,
    )
    row = _make_blind_row(model="claude-haiku-4-5", model_config=drifted)
    assert versus_judge.judge_config_is_current(row, "general_quality", cfg=versus_cfg) is False


def test_blind_row_with_unexpected_thinking_is_stale(versus_cfg):
    mc = get_judge_model_config("claude-haiku-4-5", cfg=versus_cfg)
    drifted = ModelConfig(
        temperature=mc.temperature,
        max_tokens=mc.max_tokens,
        thinking={"type": "adaptive"},
        effort=mc.effort,
    )
    row = _make_blind_row(model="claude-haiku-4-5", model_config=drifted)
    assert versus_judge.judge_config_is_current(row, "general_quality", cfg=versus_cfg) is False


def test_orch_row_for_opus_with_registry_config_is_current(versus_cfg):
    mc = get_judge_model_config("claude-opus-4-7", cfg=versus_cfg)
    row = _make_orch_row(model="claude-opus-4-7", model_config=mc)
    assert versus_judge.judge_config_is_current(row, "general_quality", cfg=versus_cfg) is True


def test_orch_row_for_opus_with_dropped_thinking_is_stale(versus_cfg):
    # Captured thinking=None but registry says opus 4.7 should run with
    # adaptive thinking. Stale because the row's recorded condition no
    # longer matches what versus would send today.
    mc = get_judge_model_config("claude-opus-4-7", cfg=versus_cfg)
    drifted = ModelConfig(
        temperature=mc.temperature,
        max_tokens=mc.max_tokens,
        thinking=None,
        effort=mc.effort,
    )
    row = _make_orch_row(model="claude-opus-4-7", model_config=drifted)
    assert versus_judge.judge_config_is_current(row, "general_quality", cfg=versus_cfg) is False


def test_orch_row_for_opus_with_dropped_effort_is_stale(versus_cfg):
    mc = get_judge_model_config("claude-opus-4-7", cfg=versus_cfg)
    drifted = ModelConfig(
        temperature=mc.temperature,
        max_tokens=mc.max_tokens,
        thinking=mc.thinking,
        effort=None,
    )
    row = _make_orch_row(model="claude-opus-4-7", model_config=drifted)
    assert versus_judge.judge_config_is_current(row, "general_quality", cfg=versus_cfg) is False


def test_unrelated_dimension_returns_false(versus_cfg):
    mc = get_judge_model_config("claude-haiku-4-5", cfg=versus_cfg)
    row = _make_blind_row(model="claude-haiku-4-5", model_config=mc)
    # An unknown dimension makes compute_judge_prompt_hash raise; helper
    # returns False so unknown rows show up as stale rather than crashing.
    assert versus_judge.judge_config_is_current(row, "no-such-dimension", cfg=versus_cfg) is False


def test_unknown_model_returns_false(versus_cfg):
    # Row references a model that's been removed from the registry —
    # versus can't reproduce the config, treat as stale.
    mc = get_judge_model_config("claude-haiku-4-5", cfg=versus_cfg)
    row = _make_blind_row(model="not-a-registered-model", model_config=mc)
    assert versus_judge.judge_config_is_current(row, "general_quality", cfg=versus_cfg) is False
