"""Tests for single-call baseline + calibration eval.

All LLM access is mocked. No real API calls.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from rumil.llm import StructuredCallResult
from rumil.models import (
    ConsiderationDirection,
    LinkType,
    Page,
    PageLayer,
    PageLink,
    PageType,
    Workspace,
)
from rumil.run_eval.baselines import (
    BaselineView,
    HeadlineClaim,
    SingleCallBaselineResult,
    Uncertainty,
    render_baseline_view,
    run_single_call_baseline,
)
from rumil.run_eval.calibration import (
    CredenceComparison,
    classify_calibration,
    compute_calibration_score,
    format_comparisons_markdown,
    overconfidence_delta,
)
from rumil.run_eval.runner import _maybe_run_single_call_baseline, build_system_prompt
from rumil.settings import override_settings


@pytest_asyncio.fixture
async def question_with_considerations(tmp_db):
    """Root question plus two considerations (one with credence, one without)."""
    question = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="Will models automate routine cognitive labour before 2030?",
        headline="Will models automate routine cognitive labour before 2030?",
    )
    await tmp_db.save_page(question)

    claim_a = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="Routine cognitive labour already partly automated in coding workflows.",
        headline="Coding workflows already partly automated",
        credence=7,
        robustness=3,
    )
    await tmp_db.save_page(claim_a)
    await tmp_db.save_link(
        PageLink(
            from_page_id=claim_a.id,
            to_page_id=question.id,
            link_type=LinkType.CONSIDERATION,
            direction=ConsiderationDirection.SUPPORTS,
        )
    )

    claim_b = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="Regulatory friction could slow deployment in large enterprises.",
        headline="Regulatory friction slows deployment",
    )
    await tmp_db.save_page(claim_b)
    await tmp_db.save_link(
        PageLink(
            from_page_id=claim_b.id,
            to_page_id=question.id,
            link_type=LinkType.CONSIDERATION,
            direction=ConsiderationDirection.OPPOSES,
        )
    )

    return question


def _make_baseline_view() -> BaselineView:
    return BaselineView(
        headline="Partial automation by 2030 is likely.",
        summary="Synthesis paragraph.",
        claims=[
            HeadlineClaim(claim="Coding is already partly automated.", credence=7),
            HeadlineClaim(claim="Full automation by 2030 remains uncertain.", credence=4),
        ],
        uncertainties=[Uncertainty(description="Regulatory response timing.")],
    )


def _structured_result(parsed: BaselineView | None) -> StructuredCallResult[BaselineView]:
    return StructuredCallResult(
        parsed=parsed,
        response_text="mocked response",
        input_tokens=1234,
        output_tokens=567,
        duration_ms=100,
    )


async def test_baseline_returns_well_shaped_result(
    tmp_db,
    question_with_considerations,
    mocker,
):
    view = _make_baseline_view()
    mock = mocker.patch(
        "rumil.run_eval.baselines.structured_call",
        new=AsyncMock(return_value=_structured_result(view)),
    )

    result = await run_single_call_baseline(
        tmp_db,
        question_with_considerations.id,
        model="claude-opus-4-7",
    )

    assert isinstance(result, SingleCallBaselineResult)
    assert result.question_id == question_with_considerations.id
    assert result.model == "claude-opus-4-7"
    assert result.view is not None
    assert result.view.headline == view.headline
    assert result.input_tokens == 1234
    assert result.output_tokens == 567
    assert result.cost_usd > 0
    assert result.call_id
    assert question_with_considerations.id in result.context_page_ids
    mock.assert_called_once()


async def test_baseline_handles_no_parsed_view(
    tmp_db,
    question_with_considerations,
    mocker,
):
    """Baseline still succeeds when structured parsing failed."""
    mocker.patch(
        "rumil.run_eval.baselines.structured_call",
        new=AsyncMock(return_value=_structured_result(None)),
    )

    result = await run_single_call_baseline(
        tmp_db,
        question_with_considerations.id,
    )

    assert result.view is None
    assert result.response_text == "mocked response"
    assert result.call_id


async def test_baseline_rejects_non_question(tmp_db, mocker):
    claim = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="Not a question.",
        headline="Not a question.",
    )
    await tmp_db.save_page(claim)
    mocker.patch(
        "rumil.run_eval.baselines.structured_call",
        new=AsyncMock(return_value=_structured_result(None)),
    )
    with pytest.raises(ValueError, match="is not a question"):
        await run_single_call_baseline(tmp_db, claim.id)


async def test_baseline_creates_call_with_single_call_baseline_type(
    tmp_db,
    question_with_considerations,
    mocker,
):
    mocker.patch(
        "rumil.run_eval.baselines.structured_call",
        new=AsyncMock(return_value=_structured_result(_make_baseline_view())),
    )
    result = await run_single_call_baseline(
        tmp_db,
        question_with_considerations.id,
    )
    assert result.call_id is not None
    call = await tmp_db.get_call(result.call_id)
    assert call is not None
    assert call.call_type.value == "single_call_baseline"


async def test_baseline_fires_when_setting_enabled(
    tmp_db,
    question_with_considerations,
    mocker,
):
    mock = mocker.patch(
        "rumil.run_eval.baselines.structured_call",
        new=AsyncMock(return_value=_structured_result(_make_baseline_view())),
    )
    with override_settings(eval_include_single_call_baseline=True):
        result = await _maybe_run_single_call_baseline(
            question_with_considerations.id,
            tmp_db,
            broadcaster=None,
        )
    assert result is not None
    assert result.view is not None
    mock.assert_called_once()


async def test_baseline_skipped_when_setting_disabled(
    tmp_db,
    question_with_considerations,
    mocker,
):
    mock = mocker.patch(
        "rumil.run_eval.baselines.structured_call",
        new=AsyncMock(return_value=_structured_result(_make_baseline_view())),
    )
    with override_settings(eval_include_single_call_baseline=False):
        result = await _maybe_run_single_call_baseline(
            question_with_considerations.id,
            tmp_db,
            broadcaster=None,
        )
    assert result is None
    mock.assert_not_called()


async def test_baseline_swallows_errors_when_toggled_on(
    tmp_db,
    question_with_considerations,
    mocker,
):
    """If the baseline call blows up, eval should log and continue — not crash."""
    mocker.patch(
        "rumil.run_eval.baselines.structured_call",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    )
    with override_settings(eval_include_single_call_baseline=True):
        result = await _maybe_run_single_call_baseline(
            question_with_considerations.id,
            tmp_db,
            broadcaster=None,
        )
    assert result is None


def test_render_baseline_view_handles_none():
    assert "produced no output" in render_baseline_view(None, "")
    assert render_baseline_view(None, "raw response") == "raw response"


def test_render_baseline_view_renders_full_view():
    view = _make_baseline_view()
    rendered = render_baseline_view(view, "")
    assert view.headline in rendered
    assert "C7" in rendered
    assert "Regulatory response timing" in rendered


def test_calibration_score_perfect_agreement():
    comps = [
        CredenceComparison(claim_id="a", headline="h", self_credence=7, reviewer_credence=7),
        CredenceComparison(claim_id="b", headline="h", self_credence=3, reviewer_credence=3),
    ]
    score = compute_calibration_score(comps)
    assert score == 1.0


def test_calibration_score_max_disagreement():
    comps = [
        CredenceComparison(claim_id="a", headline="h", self_credence=1, reviewer_credence=9),
    ]
    score = compute_calibration_score(comps)
    assert score == 0.0


@pytest.mark.parametrize(
    ("self_c", "rev_c", "expected"),
    [
        (7, 6, 0.875),
        (5, 5, 1.0),
        (9, 5, 0.5),
        (1, 9, 0.0),
    ],
)
def test_calibration_score_per_sample_math(self_c, rev_c, expected):
    comps = [
        CredenceComparison(
            claim_id="a",
            headline="h",
            self_credence=self_c,
            reviewer_credence=rev_c,
        )
    ]
    score = compute_calibration_score(comps)
    assert score is not None
    assert score == pytest.approx(expected)


def test_calibration_score_is_always_in_unit_interval():
    comps = [
        CredenceComparison(claim_id=str(i), headline="h", self_credence=s, reviewer_credence=r)
        for i, (s, r) in enumerate([(1, 9), (5, 5), (9, 1), (4, 7), (7, 4)])
    ]
    score = compute_calibration_score(comps)
    assert score is not None
    assert 0.0 <= score <= 1.0


def test_calibration_handles_missing_self_credence():
    comps = [
        CredenceComparison(claim_id="a", headline="h", self_credence=None, reviewer_credence=7),
        CredenceComparison(claim_id="b", headline="h", self_credence=6, reviewer_credence=6),
    ]
    score = compute_calibration_score(comps)
    assert score == 1.0


def test_calibration_handles_missing_reviewer_credence():
    comps = [
        CredenceComparison(claim_id="a", headline="h", self_credence=7, reviewer_credence=None),
        CredenceComparison(claim_id="b", headline="h", self_credence=6, reviewer_credence=6),
    ]
    score = compute_calibration_score(comps)
    assert score == 1.0


def test_calibration_returns_none_when_all_samples_unusable():
    comps = [
        CredenceComparison(claim_id="a", headline="h", self_credence=None, reviewer_credence=7),
        CredenceComparison(claim_id="b", headline="h", self_credence=6, reviewer_credence=None),
    ]
    assert compute_calibration_score(comps) is None


def test_calibration_returns_none_for_empty_input():
    assert compute_calibration_score([]) is None


def test_calibration_rejects_out_of_range_credence():
    comps = [
        CredenceComparison(claim_id="a", headline="h", self_credence=12, reviewer_credence=5),
    ]
    assert compute_calibration_score(comps) is None


def test_overconfidence_delta_positive_means_overconfident():
    comps = [
        CredenceComparison(claim_id="a", headline="h", self_credence=8, reviewer_credence=5),
        CredenceComparison(claim_id="b", headline="h", self_credence=7, reviewer_credence=5),
    ]
    delta = overconfidence_delta(comps)
    assert delta is not None
    assert delta > 0


def test_overconfidence_delta_negative_means_underconfident():
    comps = [
        CredenceComparison(claim_id="a", headline="h", self_credence=4, reviewer_credence=7),
    ]
    delta = overconfidence_delta(comps)
    assert delta is not None
    assert delta < 0


def test_overconfidence_delta_none_on_empty():
    assert overconfidence_delta([]) is None


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        (None, "insufficient data"),
        (1.0, "well-calibrated"),
        (0.92, "well-calibrated"),
        (0.8, "modestly calibrated"),
        (0.6, "noticeably off"),
        (0.1, "poorly calibrated"),
    ],
)
def test_classify_calibration(score, expected):
    assert classify_calibration(score) == expected


def test_format_comparisons_markdown_renders_table():
    comps = [
        CredenceComparison(
            claim_id=str(uuid.uuid4()),
            headline="Claim A",
            self_credence=7,
            reviewer_credence=5,
        ),
        CredenceComparison(
            claim_id=str(uuid.uuid4()),
            headline="Claim B",
            self_credence=None,
            reviewer_credence=None,
        ),
    ]
    rendered = format_comparisons_markdown(comps)
    assert "Claim A" in rendered
    assert "Claim B" in rendered
    assert "|" in rendered


def test_format_comparisons_markdown_on_empty():
    assert "No claims sampled" in format_comparisons_markdown([])


def test_calibration_prompt_loads_with_other_dimensions_expanded():
    from rumil.run_eval.agents import EVAL_AGENTS

    specs = {s.name: s for s in EVAL_AGENTS}
    calibration_spec = specs["calibration"]
    prompt = build_system_prompt(calibration_spec, all_agents=EVAL_AGENTS)
    assert "Calibration" in prompt
    assert "{other_dimensions}" not in prompt
    assert "Grounding & Factual Correctness" in prompt
    assert "Consistency" in prompt


def test_calibration_prompt_standalone_gets_placeholder_fallback():
    """When the calibration agent is the only one being run, the other-dims
    section should get a graceful fallback."""
    from rumil.run_eval.agents import EVAL_AGENTS

    specs = {s.name: s for s in EVAL_AGENTS}
    calibration_spec = specs["calibration"]
    prompt = build_system_prompt(calibration_spec, all_agents=[calibration_spec])
    assert "{other_dimensions}" not in prompt
    assert "No other dimensions" in prompt


def test_calibration_agent_registered():
    from rumil.run_eval.agents import EVAL_AGENTS

    names = [s.name for s in EVAL_AGENTS]
    assert "calibration" in names
    spec = next(s for s in EVAL_AGENTS if s.name == "calibration")
    assert spec.prompt_file == "run-eval-calibration.md"
