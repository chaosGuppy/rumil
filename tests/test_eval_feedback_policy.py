"""Behavioural tests for ``EvalFeedbackPolicy`` — the hedges mandate that
``eval_feedback_enabled=False`` suppresses everything, a page's own eval
never drives a dispatch against itself, and repeat dispatches on the
same target get progressively deprioritized via decay.
"""

from __future__ import annotations

import pytest_asyncio

from rumil.models import Call, CallStatus, CallType, Page, PageLayer, PageType, Workspace
from rumil.orchestrators.policies.eval_feedback import EvalFeedbackPolicy
from rumil.orchestrators.policy_layer import DispatchCall, QuestionState
from rumil.settings import get_settings


def _make_state(
    *,
    question_id: str,
    consideration_ids: tuple[str, ...],
) -> QuestionState:
    return QuestionState(
        question_id=question_id,
        budget_remaining=10,
        iteration=0,
        consideration_count=len(consideration_ids),
        child_question_count=0,
        source_count=0,
        view=None,
        missing_credence_page_ids=(),
        missing_importance_item_ids=(),
        unjudged_child_question_ids=(),
        recent_call_types=(),
        consideration_page_ids=consideration_ids,
        child_question_ids=(),
    )


@pytest_asyncio.fixture
async def eval_db(tmp_db):
    """tmp_db plus a run row so reputation_events FK is satisfied."""
    await tmp_db.create_run(name="eval-feedback-test", question_id=None, config={})
    return tmp_db


@pytest_asyncio.fixture
async def claim_page(eval_db, question_page):
    page = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="GPU price-performance doubles every 2.5 years.",
        headline="GPU price-performance doubling time",
    )
    await eval_db.save_page(page)
    return page


async def _emit_grounding(db, page_id: str, score: float, source_call_id: str | None = None):
    await db.record_reputation_event(
        source="run_eval",
        dimension="grounding",
        score=score,
        source_call_id=source_call_id,
        extra={"subject_page_id": page_id, "subject_run_id": db.run_id},
    )


async def test_kill_switch_returns_none(eval_db, question_page, claim_page):
    await _emit_grounding(eval_db, claim_page.id, 0.05)
    await _emit_grounding(eval_db, claim_page.id, 0.10)

    policy = EvalFeedbackPolicy(eval_db)
    state = _make_state(
        question_id=question_page.id,
        consideration_ids=(claim_page.id,),
    )

    assert not get_settings().eval_feedback_enabled, "default must be off"
    assert await policy.decide(state) is None


async def test_no_events_returns_none(eval_db, question_page, claim_page, mocker):
    mocker.patch.object(get_settings(), "eval_feedback_enabled", True)
    policy = EvalFeedbackPolicy(eval_db)
    state = _make_state(
        question_id=question_page.id,
        consideration_ids=(claim_page.id,),
    )
    assert await policy.decide(state) is None


async def test_low_grounding_returns_dispatch(eval_db, question_page, claim_page, mocker):
    mocker.patch.object(get_settings(), "eval_feedback_enabled", True)
    mocker.patch.object(get_settings(), "eval_feedback_grounding_floor", 0.4)
    mocker.patch.object(get_settings(), "eval_feedback_min_event_count", 2)

    await _emit_grounding(eval_db, claim_page.id, 0.10)
    await _emit_grounding(eval_db, claim_page.id, 0.20)

    policy = EvalFeedbackPolicy(eval_db)
    state = _make_state(
        question_id=question_page.id,
        consideration_ids=(claim_page.id,),
    )
    intent = await policy.decide(state)
    assert isinstance(intent, DispatchCall)
    assert intent.call_type == CallType.ASSESS
    assert intent.kwargs["question_id"] == claim_page.id


async def test_min_event_count_gate(eval_db, question_page, claim_page, mocker):
    mocker.patch.object(get_settings(), "eval_feedback_enabled", True)
    mocker.patch.object(get_settings(), "eval_feedback_grounding_floor", 0.4)
    mocker.patch.object(get_settings(), "eval_feedback_min_event_count", 3)
    await _emit_grounding(eval_db, claim_page.id, 0.05)
    await _emit_grounding(eval_db, claim_page.id, 0.10)
    policy = EvalFeedbackPolicy(eval_db)
    state = _make_state(
        question_id=question_page.id,
        consideration_ids=(claim_page.id,),
    )
    assert await policy.decide(state) is None


async def test_decay_on_repeat_dispatch(eval_db, question_page, claim_page, mocker):
    mocker.patch.object(get_settings(), "eval_feedback_enabled", True)
    mocker.patch.object(get_settings(), "eval_feedback_grounding_floor", 0.4)
    mocker.patch.object(get_settings(), "eval_feedback_min_event_count", 2)
    await _emit_grounding(eval_db, claim_page.id, 0.10)
    await _emit_grounding(eval_db, claim_page.id, 0.30)

    policy = EvalFeedbackPolicy(eval_db)
    state = _make_state(
        question_id=question_page.id,
        consideration_ids=(claim_page.id,),
    )
    first = await policy.decide(state)
    assert isinstance(first, DispatchCall)
    assert policy._dispatch_counts[claim_page.id] == 1

    second = await policy.decide(state)
    assert isinstance(second, DispatchCall)
    assert policy._dispatch_counts[claim_page.id] == 2


async def test_self_eval_filter(eval_db, question_page, claim_page, mocker):
    mocker.patch.object(get_settings(), "eval_feedback_enabled", True)
    mocker.patch.object(get_settings(), "eval_feedback_grounding_floor", 0.4)
    mocker.patch.object(get_settings(), "eval_feedback_min_event_count", 1)

    own_call = Call(
        call_type=CallType.ASSESS,
        workspace=Workspace.RESEARCH,
        scope_page_id=claim_page.id,
        status=CallStatus.COMPLETE,
    )
    await eval_db.save_call(own_call)
    await _emit_grounding(eval_db, claim_page.id, 0.05, source_call_id=own_call.id)

    policy = EvalFeedbackPolicy(eval_db)
    state = _make_state(
        question_id=question_page.id,
        consideration_ids=(claim_page.id,),
    )
    assert await policy.decide(state) is None
