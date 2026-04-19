"""Tests for ``select_lazy_eval_targets`` — the pure per-round selector
that picks which pages deserve a cheap lazy eval before scoring.
"""

from __future__ import annotations

from rumil.database import EvalSummary
from rumil.eval_feedback import LAZY_EVAL_DIMENSIONS, select_lazy_eval_targets


def _sum(dim: str, *, count: int) -> EvalSummary:
    return EvalSummary(dimension=dim, mean=0.5, count=count, latest=0.5)


def test_picks_pages_missing_any_dimension():
    out = select_lazy_eval_targets(
        ["a", "b", "c"],
        summaries={
            "a": {
                "grounding": _sum("grounding", count=2),
                "calibration": _sum("calibration", count=1),
            },
            "b": {"grounding": _sum("grounding", count=3)},  # missing calibration
            "c": {},  # missing both
        },
        per_round_cap=10,
        already_evaluated_this_run=0,
        per_run_cap=20,
    )
    assert out == ["b", "c"]


def test_respects_per_round_cap():
    out = select_lazy_eval_targets(
        ["a", "b", "c", "d"],
        summaries={p: {} for p in ("a", "b", "c", "d")},
        per_round_cap=2,
        already_evaluated_this_run=0,
        per_run_cap=20,
    )
    assert out == ["a", "b"]


def test_respects_per_run_cap():
    out = select_lazy_eval_targets(
        ["a", "b", "c"],
        summaries={p: {} for p in ("a", "b", "c")},
        per_round_cap=10,
        already_evaluated_this_run=18,
        per_run_cap=20,
    )
    # only 2 left in the per-run budget
    assert out == ["a", "b"]


def test_zero_cap_returns_empty():
    out = select_lazy_eval_targets(
        ["a", "b"],
        summaries={},
        per_round_cap=0,
        already_evaluated_this_run=0,
        per_run_cap=20,
    )
    assert out == []


def test_exhausted_per_run_cap_returns_empty():
    out = select_lazy_eval_targets(
        ["a", "b"],
        summaries={},
        per_round_cap=10,
        already_evaluated_this_run=20,
        per_run_cap=20,
    )
    assert out == []


def test_preserves_input_order():
    out = select_lazy_eval_targets(
        ["z", "y", "x"],
        summaries={p: {} for p in ("z", "y", "x")},
        per_round_cap=3,
        already_evaluated_this_run=0,
        per_run_cap=20,
    )
    assert out == ["z", "y", "x"]


def test_zero_count_summary_counts_as_missing():
    out = select_lazy_eval_targets(
        ["a"],
        summaries={
            "a": {
                "grounding": EvalSummary(dimension="grounding", mean=0.0, count=0, latest=0.0),
                "calibration": _sum("calibration", count=1),
            }
        },
        per_round_cap=5,
        already_evaluated_this_run=0,
        per_run_cap=20,
    )
    assert out == ["a"]


def test_covered_page_skipped():
    covered = {dim: _sum(dim, count=1) for dim in LAZY_EVAL_DIMENSIONS}
    out = select_lazy_eval_targets(
        ["a", "b"],
        summaries={"a": covered, "b": {}},
        per_round_cap=5,
        already_evaluated_this_run=0,
        per_run_cap=20,
    )
    assert out == ["b"]
