"""Guard the breadth-vs-depth fix from problem #5 of the wave 7 eval.

Two protections:

* \\***Shape A (prompts)\\***: the depth-priority rule is present in the two
  main-phase prioritization prompt files, with the key concepts spelled out
  (load-bearing, unresolved, covered-superficially ≠ resolved).
* \\***Shape B (scoring)\\***: ``compute_priority_score`` accepts an optional
  ``unresolved_load_bearing_weight`` and actually lifts a deep-on-load-bearing
  candidate above a shallow-but-wide candidate that would otherwise score
  higher under the base formula.
"""

from __future__ import annotations

import pytest

from rumil.llm import _load_file
from rumil.orchestrators.common import compute_priority_score

_MAIN_PHASE_PROMPT_FILES = (
    "two_phase_main_phase_prioritization.md",
    "claim_investigation_p2.md",
)


@pytest.mark.parametrize("filename", _MAIN_PHASE_PROMPT_FILES)
def test_depth_priority_rule_present(filename: str) -> None:
    text = _load_file(filename)
    assert "Depth priority" in text
    assert "load-bearing unresolved" in text
    # Some prompt files escape underscores for their target markdown flavour;
    # accept either form.
    assert "DEPENDS_ON" in text or "DEPENDS\\_ON" in text


@pytest.mark.parametrize("filename", _MAIN_PHASE_PROMPT_FILES)
def test_covered_superficially_not_resolved_rule_present(filename: str) -> None:
    text = _load_file(filename)
    assert "Covered superficially" in text
    assert "resolved" in text


def test_base_formula_unchanged_when_weight_is_default() -> None:
    shallow = compute_priority_score(5, 5, 8)
    deep = compute_priority_score(7, 7, 5)
    assert shallow == 13
    assert deep == 16


def test_deep_load_bearing_beats_shallow_wide_with_weight() -> None:
    # Wave-7 scenario: a shallow-but-wide fresh candidate at (5, 5, 8) scores
    # slightly below a deep-on-load-bearing-unresolved candidate at (7, 7, 5)
    # in the base formula (13 vs 16 — a ~23% edge, inside LLM scoring noise).
    # With the load-bearing-unresolved weight applied to the latter, the deep
    # candidate pulls clear of the shallow one.
    shallow_wide = compute_priority_score(
        impact_on_question=5,
        broader_impact=5,
        fruit=8,
        unresolved_load_bearing_weight=0.0,
    )
    deep_load_bearing = compute_priority_score(
        impact_on_question=7,
        broader_impact=7,
        fruit=5,
        unresolved_load_bearing_weight=1.0,
    )
    assert shallow_wide == 13
    assert deep_load_bearing > shallow_wide
    # Base-formula edge for the deep candidate was only 3 points (16 - 13);
    # with the weight it should widen meaningfully.
    assert deep_load_bearing - shallow_wide >= 10


def test_weight_monotonic_in_load_bearing_unresolved() -> None:
    args = dict(impact_on_question=6, broader_impact=6, fruit=6)
    low = compute_priority_score(**args, unresolved_load_bearing_weight=0.0)
    mid = compute_priority_score(**args, unresolved_load_bearing_weight=0.5)
    high = compute_priority_score(**args, unresolved_load_bearing_weight=1.0)
    assert low <= mid <= high
    assert high > low


def test_weight_is_clamped_to_unit_interval() -> None:
    args = dict(impact_on_question=6, broader_impact=6, fruit=6)
    at_one = compute_priority_score(**args, unresolved_load_bearing_weight=1.0)
    above_one = compute_priority_score(**args, unresolved_load_bearing_weight=5.0)
    at_zero = compute_priority_score(**args, unresolved_load_bearing_weight=0.0)
    below_zero = compute_priority_score(**args, unresolved_load_bearing_weight=-2.0)
    assert at_one == above_one
    assert at_zero == below_zero
