"""Tests for the order axis on versus judgments.

The order field (``"ab"`` / ``"ba"``) records which orientation the judge
saw a pair in: ``"ab"`` when the alphabetically-lower source was shown
as Continuation A. It survives on the judge_inputs blob and is computed
from display_first vs sorted(source_a, source_b).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

_VERSUS_SRC = Path(__file__).resolve().parents[1] / "versus" / "src"
if str(_VERSUS_SRC) not in sys.path:
    sys.path.insert(0, str(_VERSUS_SRC))

from versus.rumil_judge import _mirror_row, _PendingPair  # noqa: E402

from versus import judge as versus_judge  # noqa: E402

_STUB_CONFIG = {"variant": "orch", "model": "stub"}


# order_from_display_first correctness --------------------------------------


def test_order_from_display_first_ab_when_alphabetically_lower_is_first():
    # sorted(["human", "openai/gpt-5.4"])[0] == "human"
    assert versus_judge.order_from_display_first("human", "openai/gpt-5.4", "human") == "ab"


def test_order_from_display_first_ba_when_alphabetically_higher_is_first():
    # sorted(["human", "openai/gpt-5.4"])[0] == "human"; first is the other one
    assert (
        versus_judge.order_from_display_first("human", "openai/gpt-5.4", "openai/gpt-5.4") == "ba"
    )


def test_order_from_display_first_is_symmetric_in_input_order():
    # The arg order shouldn't matter -- what matters is which *sorted*
    # slot the display_first sits in.
    assert versus_judge.order_from_display_first(
        "human", "paraphrase:foo", "human"
    ) == versus_judge.order_from_display_first("paraphrase:foo", "human", "human")


# _mirror_row includes the order field --------------------------------------


@dataclass
class _FakeJudgeResult:
    verdict: str | None = "A"
    preference_label: str | None = "A strongly preferred"
    reasoning_text: str = "because."
    trace_url: str = "http://x/traces/r"
    call_id: str = "call-1"
    run_id: str = "run-1"
    question_id: str = "q-1"
    cost_usd: float = 0.0


def _make_pending_pair(source_a_id: str, source_b_id: str, display_first_id: str) -> _PendingPair:
    other = source_b_id if display_first_id == source_a_id else source_a_id
    return _PendingPair(
        essay_id="essay-1",
        prefix_hash="prefix-1",
        prefix_text="",
        source_a_id=source_a_id,
        source_a_text="A-text",
        source_a_text_id="text-a-uuid",
        source_b_id=source_b_id,
        source_b_text="B-text",
        source_b_text_id="text-b-uuid",
        display_first_id=display_first_id,
        display_first_text="A-text" if display_first_id == source_a_id else "B-text",
        display_second_id=other,
        display_second_text="B-text" if display_first_id == source_a_id else "A-text",
    )


def test_mirror_row_carries_order_ab_when_lower_is_first():
    pair = _make_pending_pair("human", "openai/gpt-5.4", display_first_id="human")
    row = _mirror_row(
        pair,
        "rumil:text:m:d:p1:v1:s1",
        "rumil_d",
        _FakeJudgeResult(),
        t0=0.0,
        judge_inputs=dict(_STUB_CONFIG),
        variant="orch",
    )
    assert row["judge_inputs"]["order"] == "ab"


def test_mirror_row_carries_order_ba_when_higher_is_first():
    pair = _make_pending_pair("human", "openai/gpt-5.4", display_first_id="openai/gpt-5.4")
    row = _mirror_row(
        pair,
        "rumil:text:m:d:p1:v1:s1",
        "rumil_d",
        _FakeJudgeResult(),
        t0=0.0,
        judge_inputs=dict(_STUB_CONFIG),
        variant="orch",
    )
    assert row["judge_inputs"]["order"] == "ba"


def test_mirror_row_threads_text_ids_into_judge_inputs():
    # text_a_id / text_b_id baked into judge_inputs so re-judging different
    # completion samples naturally forks the hash.
    pair = _make_pending_pair("human", "openai/gpt-5.4", display_first_id="human")
    row = _mirror_row(
        pair,
        "rumil:text:m:d:p1:v1:s1",
        "rumil_d",
        _FakeJudgeResult(),
        t0=0.0,
        judge_inputs=dict(_STUB_CONFIG),
        variant="orch",
    )
    assert row["judge_inputs"]["text_a_id"] == "text-a-uuid"
    assert row["judge_inputs"]["text_b_id"] == "text-b-uuid"
    assert row["text_a_id"] == "text-a-uuid"
    assert row["text_b_id"] == "text-b-uuid"
