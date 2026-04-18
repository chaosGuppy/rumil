"""Tests for A/B eval position-bias correction (B05) and structured preference extraction (B07)."""

import random
from collections import Counter

import pytest

from rumil.ab_eval import runner as ab_runner
from rumil.ab_eval.runner import (
    _deswap_preference,
    _extract_preference_structured,
    _PreferenceExtraction,
    _run_comparison,
)
from rumil.llm import LLMExchangeMetadata, StructuredCallResult
from rumil.models import Call, CallStatus, CallType, Workspace
from rumil.run_eval.agents import EvalAgentSpec


@pytest.fixture
def spec() -> EvalAgentSpec:
    return EvalAgentSpec(
        name="testdim",
        display_name="Test Dimension",
        prompt_file="run-eval-grounding.md",
    )


def _make_call() -> Call:
    return Call(
        call_type=CallType.AB_EVAL_COMPARISON,
        workspace=Workspace.RESEARCH,
        status=CallStatus.PENDING,
    )


@pytest.mark.parametrize(
    ("prompt_pref", "a_was_first", "expected"),
    [
        ("A strongly preferred", True, "A strongly preferred"),
        ("A strongly preferred", False, "B strongly preferred"),
        ("B somewhat preferred", False, "A somewhat preferred"),
        ("A slightly preferred", False, "B slightly preferred"),
        (
            "Approximately indifferent between A and B",
            False,
            "Approximately indifferent between A and B",
        ),
        (
            "Approximately indifferent between A and B",
            True,
            "Approximately indifferent between A and B",
        ),
    ],
)
def test_deswap_preference(prompt_pref, a_was_first, expected):
    assert _deswap_preference(prompt_pref, a_was_first) == expected


async def test_position_bias_is_neutralized(mocker, spec):
    """If the model always picks the A-slot, the returned preference should split ~50/50
    across the caller's two arms because `_run_comparison` de-swaps based on slot assignment."""

    async def fake_traced_text_call(db, **kwargs):
        return "A strongly preferred\n\n(reasoning...)", _make_call()

    async def fake_extract(comparison_text, db, metadata):
        return "A strongly preferred"

    mocker.patch.object(ab_runner, "_traced_text_call", side_effect=fake_traced_text_call)
    mocker.patch.object(ab_runner, "_extract_preference_structured", side_effect=fake_extract)

    rng = random.Random(42)
    n = 200
    preferences: list[str] = []
    a_first_flags: list[bool] = []
    for _ in range(n):
        _text, pref, _call, a_was_first = await _run_comparison(
            spec,
            report_a="report from caller's A arm",
            report_b="report from caller's B arm",
            db=mocker.MagicMock(),
            scope_page_id=None,
            broadcaster=None,
            rng=rng,
        )
        preferences.append(pref)
        a_first_flags.append(a_was_first)

    counts = Counter(preferences)
    flag_counts = Counter(a_first_flags)
    a_pref = counts["A strongly preferred"]
    b_pref = counts["B strongly preferred"]
    assert a_pref + b_pref == n
    assert 0.35 * n < a_pref < 0.65 * n, f"a_pref={a_pref}, counts={counts}"
    assert 0.35 * n < b_pref < 0.65 * n, f"b_pref={b_pref}, counts={counts}"
    assert 0.35 * n < flag_counts[True] < 0.65 * n


async def test_position_bias_without_swap_all_A(mocker, spec):
    """Sanity check: if we force `a_was_first=True` always, the model's A-slot bias
    propagates through unchanged. Confirms the fairness in the main test comes from
    randomization + de-swap, not from the mock itself."""

    async def fake_traced_text_call(db, **kwargs):
        return "A strongly preferred", _make_call()

    async def fake_extract(comparison_text, db, metadata):
        return "A strongly preferred"

    mocker.patch.object(ab_runner, "_traced_text_call", side_effect=fake_traced_text_call)
    mocker.patch.object(ab_runner, "_extract_preference_structured", side_effect=fake_extract)

    class AlwaysFirst:
        def random(self) -> float:
            return 0.0

    preferences: list[str] = []
    for _ in range(30):
        _text, pref, _call, a_was_first = await _run_comparison(
            spec,
            report_a="A",
            report_b="B",
            db=mocker.MagicMock(),
            scope_page_id=None,
            broadcaster=None,
            rng=AlwaysFirst(),  # pyright: ignore[reportArgumentType]
        )
        preferences.append(pref)
        assert a_was_first is True

    assert all(p == "A strongly preferred" for p in preferences)


async def test_prompt_slots_are_swapped_when_a_was_first_false(mocker, spec):
    """When the RNG picks a_was_first=False, the prompt content presents caller's B as Run A."""
    captured: dict[str, str] = {}

    async def fake_traced_text_call(db, **kwargs):
        captured["user_message"] = kwargs["user_message"]
        return "B strongly preferred", _make_call()

    async def fake_extract(comparison_text, db, metadata):
        return "B strongly preferred"

    mocker.patch.object(ab_runner, "_traced_text_call", side_effect=fake_traced_text_call)
    mocker.patch.object(ab_runner, "_extract_preference_structured", side_effect=fake_extract)

    class AlwaysSwap:
        def random(self) -> float:
            return 0.99

    _text, pref, _call, a_was_first = await _run_comparison(
        spec,
        report_a="AAA_CALLER_REPORT",
        report_b="BBB_CALLER_REPORT",
        db=mocker.MagicMock(),
        scope_page_id=None,
        broadcaster=None,
        rng=AlwaysSwap(),  # pyright: ignore[reportArgumentType]
    )

    assert a_was_first is False
    msg = captured["user_message"]
    run_a_idx = msg.index("## Run A Report")
    run_b_idx = msg.index("## Run B Report")
    run_a_section = msg[run_a_idx:run_b_idx]
    run_b_section = msg[run_b_idx:]
    assert "BBB_CALLER_REPORT" in run_a_section
    assert "AAA_CALLER_REPORT" in run_b_section
    assert pref == "A strongly preferred"


async def test_extract_preference_structured_returns_label(mocker):
    """The structured extractor should return whatever label the LLM chose, via pydantic."""
    parsed = _PreferenceExtraction(preference="B somewhat preferred")
    fake_result = StructuredCallResult(parsed=parsed, response_text="{}")

    async def fake_structured_call(system, user, *, response_model, **kwargs):
        assert response_model is _PreferenceExtraction
        return fake_result

    mocker.patch.object(ab_runner, "structured_call", side_effect=fake_structured_call)

    result = await _extract_preference_structured(
        "Some comparison text that concludes with B somewhat preferred.",
        db=mocker.MagicMock(),
        metadata=LLMExchangeMetadata(call_id="c1", phase="p"),
    )
    assert result == "B somewhat preferred"


async def test_extract_preference_structured_raises_on_no_parse(mocker):
    fake_result: StructuredCallResult[_PreferenceExtraction] = StructuredCallResult(
        parsed=None, response_text=None
    )

    async def fake_structured_call(system, user, *, response_model, **kwargs):
        return fake_result

    mocker.patch.object(ab_runner, "structured_call", side_effect=fake_structured_call)

    with pytest.raises(ValueError, match="no parseable output"):
        await _extract_preference_structured(
            "garbled",
            db=mocker.MagicMock(),
            metadata=LLMExchangeMetadata(call_id="c1", phase="p"),
        )


@pytest.mark.parametrize(
    "label",
    [
        "A strongly preferred",
        "A somewhat preferred",
        "A slightly preferred",
        "Approximately indifferent between A and B",
        "B slightly preferred",
        "B somewhat preferred",
        "B strongly preferred",
    ],
)
def test_preference_extraction_model_accepts_valid_labels(label):
    m = _PreferenceExtraction(preference=label)
    assert m.preference == label


def test_preference_extraction_model_rejects_invalid_labels():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _PreferenceExtraction(preference="A is obviously better")  # pyright: ignore[reportArgumentType]
