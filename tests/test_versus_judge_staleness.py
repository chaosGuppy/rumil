"""Tests for ``versus.judge.judge_config_is_current``.

The staleness detector fails False when any code-side input to the
judge has drifted from what a fresh run would produce — prompt shell
hash, code fingerprint (ws/orch), or thinking config (per
:func:`rumil.llm.thinking_config`).
"""

from __future__ import annotations

import pytest
from versus.judge_config import compute_judge_code_fingerprint, make_judge_config

from versus import judge as versus_judge


def _make_blind_row(*, model: str, thinking: dict | None = None, effort: str | None = None) -> dict:
    cfg, _, _ = make_judge_config(
        "blind",
        model=model,
        dimension="general_quality",
        sampling={"temperature": 0.0, "max_tokens": 1024},
        prompt_hash=versus_judge.compute_judge_prompt_hash("general_quality", with_tools=False),
        thinking=thinking,
        effort=effort,
    )
    return {"judge_inputs": cfg}


def _make_ws_row(*, model: str, thinking: dict | None, effort: str | None = None) -> dict:
    cfg, _, _ = make_judge_config(
        "ws",
        model=model,
        dimension="general_quality",
        sampling={"temperature": 0.0, "max_tokens": 1024},
        prompt_hash=versus_judge.compute_judge_prompt_hash("general_quality", with_tools=True),
        thinking=thinking,
        effort=effort,
        tool_prompt_hash="11111111",
        pair_surface_hash="22222222",
        workspace_id="abcd1234",
        code_fingerprint=compute_judge_code_fingerprint(),
        workspace_state_hash="0011223344556677",
    )
    return {"judge_inputs": cfg}


def test_blind_row_with_thinking_none_is_current():
    row = _make_blind_row(model="claude-haiku-4-5", thinking=None)
    assert versus_judge.judge_config_is_current(row, "general_quality") is True


def test_blind_row_with_unexpected_thinking_is_stale():
    row = _make_blind_row(model="claude-haiku-4-5", thinking={"type": "adaptive"})
    assert versus_judge.judge_config_is_current(row, "general_quality") is False


def test_ws_row_for_haiku_with_no_thinking_is_current():
    # haiku doesn't get adaptive thinking — recorded None matches expected None.
    row = _make_ws_row(model="claude-haiku-4-5", thinking=None)
    assert versus_judge.judge_config_is_current(row, "general_quality") is True


def test_ws_row_for_opus_47_with_adaptive_thinking_is_current():
    row = _make_ws_row(
        model="claude-opus-4-7-20251001",
        thinking={"type": "adaptive", "display": "summarized"},
        effort="xhigh",
    )
    assert versus_judge.judge_config_is_current(row, "general_quality") is True


def test_ws_row_for_opus_47_missing_thinking_is_stale():
    # An older row predating thinking-recording: thinking field missing →
    # treated as None, but expected is the adaptive dict for opus 4.7 → stale.
    row = _make_ws_row(model="claude-opus-4-7-20251001", thinking=None)
    assert versus_judge.judge_config_is_current(row, "general_quality") is False


def test_ws_row_with_wrong_thinking_dict_is_stale():
    row = _make_ws_row(
        model="claude-opus-4-7-20251001",
        thinking={"type": "adaptive"},  # missing the display field opus 4.7 sets
    )
    assert versus_judge.judge_config_is_current(row, "general_quality") is False


def test_unrelated_dimension_returns_false():
    row = _make_blind_row(model="claude-haiku-4-5", thinking=None)
    # An unknown dimension makes compute_judge_prompt_hash raise; helper
    # returns False so unknown rows show up as stale rather than crashing.
    assert versus_judge.judge_config_is_current(row, "no-such-dimension") is False


@pytest.mark.parametrize(
    ("model", "expected_thinking"),
    (
        ("claude-haiku-4-5", None),
        ("claude-opus-4-7-20251001", {"type": "adaptive", "display": "summarized"}),
        ("claude-sonnet-4-6", {"type": "adaptive"}),
    ),
)
def test_thinking_check_uses_rumil_thinking_config(model, expected_thinking):
    # Records the rumil rules at the time of writing. If thinking_config
    # changes for any of these models, this test breaks deliberately so
    # the impact on staleness semantics gets reviewed.
    from rumil.llm import thinking_config

    assert thinking_config(model) == expected_thinking


def test_blind_row_with_unexpected_effort_is_stale():
    row = _make_blind_row(model="claude-haiku-4-5", effort="high")
    assert versus_judge.judge_config_is_current(row, "general_quality") is False


def test_ws_row_for_opus_47_with_xhigh_effort_is_current():
    row = _make_ws_row(
        model="claude-opus-4-7-20251001",
        thinking={"type": "adaptive", "display": "summarized"},
        effort="xhigh",
    )
    assert versus_judge.judge_config_is_current(row, "general_quality") is True


def test_ws_row_for_opus_47_missing_effort_is_stale():
    row = _make_ws_row(
        model="claude-opus-4-7-20251001",
        thinking={"type": "adaptive", "display": "summarized"},
        effort=None,
    )
    assert versus_judge.judge_config_is_current(row, "general_quality") is False


@pytest.mark.parametrize(
    ("model", "expected_effort"),
    (
        ("claude-haiku-4-5", None),
        ("claude-opus-4-7-20251001", "xhigh"),
        ("claude-sonnet-4-6", "high"),
    ),
)
def test_effort_check_uses_rumil_effort_level(model, expected_effort):
    # Same idea as the thinking version: pin current rules so silent drift
    # in effort_level forces a deliberate test update.
    from rumil.llm import effort_level

    assert effort_level(model) == expected_effort
