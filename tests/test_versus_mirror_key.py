"""Tests for the order-axis on versus judgment_key.

Covers the key-schema change that added an ``order`` slot (``"ab"`` or
``"ba"``) so future mirror-mode aggregation can be layered in without
orphaning rows. Current enumeration still emits one task per pair; this
change only records which orientation the judge saw.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

_VERSUS_SRC = Path(__file__).resolve().parents[1] / "versus" / "src"
if str(_VERSUS_SRC) not in sys.path:
    sys.path.insert(0, str(_VERSUS_SRC))

from versus.rumil_judge import _mirror_row, _PendingPair  # noqa: E402

from versus import judge as versus_judge  # noqa: E402

# judgment_key: order slot produces distinct strings ------------------------


def test_judgment_key_ab_and_ba_produce_distinct_strings():
    args = dict(
        essay_id="e1",
        prefix_hash="ph1",
        source_a="human",
        source_b="openai/gpt-5.4",
        criterion="general_quality",
        judge_model="anthropic:claude-haiku-4-5:pdeadbeef:v1:sfeedface",
    )
    k_ab = versus_judge.judgment_key(**args, order="ab")
    k_ba = versus_judge.judgment_key(**args, order="ba")
    assert k_ab != k_ba
    assert k_ab.endswith("|ab")
    assert k_ba.endswith("|ba")


def test_judgment_key_sort_canonicalises_source_a_b():
    k1 = versus_judge.judgment_key("e", "ph", "human", "openai/gpt-5.4", "c", "jm", order="ab")
    k2 = versus_judge.judgment_key("e", "ph", "openai/gpt-5.4", "human", "c", "jm", order="ab")
    assert k1 == k2


def test_judgment_key_requires_order_as_keyword():
    with pytest.raises(TypeError):
        versus_judge.judgment_key(  # pyright: ignore[reportCallIssue]
            "e", "ph", "a", "b", "c", "jm"
        )


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


# infer_order: passthrough + legacy derivation ------------------------------


@pytest.mark.parametrize("stored", ["ab", "ba"])
def test_infer_order_passes_through_when_field_present(stored):
    row = {
        "source_a": "human",
        "source_b": "openai/gpt-5.4",
        "display_first": "openai/gpt-5.4",  # intentionally inconsistent
        "order": stored,
    }
    assert versus_judge.infer_order(row) == stored


def test_infer_order_derives_ab_from_legacy_row_with_lower_first():
    row = {
        "source_a": "human",
        "source_b": "openai/gpt-5.4",
        "display_first": "human",
    }
    assert versus_judge.infer_order(row) == "ab"


def test_infer_order_derives_ba_from_legacy_row_with_higher_first():
    row = {
        "source_a": "human",
        "source_b": "openai/gpt-5.4",
        "display_first": "openai/gpt-5.4",
    }
    assert versus_judge.infer_order(row) == "ba"


def test_infer_order_passthrough_preempts_display_first():
    # Even when display_first disagrees with source ordering, an explicit
    # stored order wins (this is what future mirror-mode rows will look
    # like -- both orders stored, display_first may vary per run).
    row = {
        "source_a": "z",
        "source_b": "a",
        "display_first": "z",  # sorted -> "a" first; display_first=="z" is "ba"
        "order": "ab",
    }
    assert versus_judge.infer_order(row) == "ab"


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


def _make_pending_pair(source_a_id: str, source_b_id: str, display_first_id: str):
    other = source_b_id if display_first_id == source_a_id else source_a_id
    return _PendingPair(
        essay_id="essay-1",
        prefix_hash="prefix-1",
        prefix_text="",
        source_a_id=source_a_id,
        source_a_text="A-text",
        source_b_id=source_b_id,
        source_b_text="B-text",
        display_first_id=display_first_id,
        display_first_text="A-text" if display_first_id == source_a_id else "B-text",
        display_second_id=other,
        display_second_text="B-text" if display_first_id == source_a_id else "A-text",
    )


def test_mirror_row_includes_order_ab_when_lower_is_first():
    pair = _make_pending_pair("human", "openai/gpt-5.4", display_first_id="human")
    row = _mirror_row(pair, "rumil:text:m:d:p1:v1:s1", "rumil_d", _FakeJudgeResult(), t0=0.0)
    assert row["order"] == "ab"
    assert row["key"].endswith("|ab")


def test_mirror_row_includes_order_ba_when_higher_is_first():
    pair = _make_pending_pair("human", "openai/gpt-5.4", display_first_id="openai/gpt-5.4")
    row = _mirror_row(pair, "rumil:text:m:d:p1:v1:s1", "rumil_d", _FakeJudgeResult(), t0=0.0)
    assert row["order"] == "ba"
    assert row["key"].endswith("|ba")


def test_mirror_row_key_matches_judgment_key():
    pair = _make_pending_pair("human", "openai/gpt-5.4", display_first_id="openai/gpt-5.4")
    judge_model = "rumil:text:m:d:p1:v1:s1"
    row = _mirror_row(pair, judge_model, "rumil_d", _FakeJudgeResult(), t0=0.0)
    expected = versus_judge.judgment_key(
        pair.essay_id,
        pair.prefix_hash,
        pair.source_a_id,
        pair.source_b_id,
        "rumil_d",
        judge_model,
        "ba",
    )
    assert row["key"] == expected


# Row-builder via the OpenRouter path ---------------------------------------


def test_call_one_blind_row_includes_order(mocker):
    mocker.patch(
        "versus.openrouter.chat",
        return_value={"choices": [{"message": {"content": "A strongly preferred"}}]},
    )
    mocker.patch("versus.openrouter.extract_text", return_value="A strongly preferred")

    from versus.judge import Source, _BlindTask, _call_one_blind

    src_a = Source("human", "A body")
    src_b = Source("openai/gpt-5.4", "B body")
    task = _BlindTask(
        essay_id="essay-1",
        prefix_hash="prefix-1",
        a_id="human",
        b_id="openai/gpt-5.4",
        first=src_a,
        second=src_b,
        dimension="general_quality",
        base_model="google/gemini-2.5-pro",
        canonical_model="google/gemini-2.5-pro",
        provider="openrouter",
        judge_model="google/gemini-2.5-pro:general_quality:p1:v1:s1",
        system_prompt="SYS",
        user_prompt="USER",
        key="fake-key",
        order="ab",
        sampling={"temperature": 0.0, "max_tokens": 2048},
    )
    row = _call_one_blind(task, client=None)  # pyright: ignore[reportArgumentType]
    assert row["order"] == "ab"
    assert row["display_first"] == "human"
