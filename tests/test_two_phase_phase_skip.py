"""Phase-skip invariants for ``TwoPhaseOrchestrator``.

When the scope question already has a judgement or a view, the
orchestrator must skip ``_initial_prioritization`` and proceed directly
to main-phase on the next loop iteration. An eagerly-created initial
call is marked complete with a ``PhaseSkippedEvent`` rather than left
PENDING. These invariants are easy to break during a rewrite because
they straddle the two-phase entry logic and the eager-call-creation
optimization.
"""

import pytest

from rumil.calls.common import RunCallResult
from rumil.models import (
    CallStatus,
    CallType,
    Dispatch,
    LinkType,
    Page,
    PageLayer,
    PageLink,
    PageType,
    ScoutDispatchPayload,
    Workspace,
)
from rumil.orchestrators.two_phase import TwoPhaseOrchestrator


def _scout_dispatch(question_id: str, reason: str = "") -> Dispatch:
    return Dispatch(
        call_type=CallType.FIND_CONSIDERATIONS,
        payload=ScoutDispatchPayload(
            question_id=question_id,
            max_rounds=1,
            reason=reason,
        ),
    )


async def _seed_judgement(db, question_id: str) -> Page:
    judgement = Page(
        page_type=PageType.JUDGEMENT,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="A provisional judgement.",
        headline="A provisional judgement.",
    )
    await db.save_page(judgement)
    await db.save_link(
        PageLink(
            from_page_id=judgement.id,
            to_page_id=question_id,
            link_type=LinkType.ANSWERS,
        )
    )
    return judgement


async def _seed_view(db, question_id: str) -> Page:
    view = Page(
        page_type=PageType.VIEW,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="A view page.",
        headline="A view page.",
    )
    await db.save_page(view)
    await db.save_link(
        PageLink(
            from_page_id=view.id,
            to_page_id=question_id,
            link_type=LinkType.VIEW_OF,
        )
    )
    return view


@pytest.mark.asyncio
async def test_initial_prio_skipped_when_judgement_exists(tmp_db, question_page, prio_harness):
    """A pre-existing judgement on the scope question causes initial prio to be skipped."""
    await _seed_judgement(tmp_db, question_page.id)
    await tmp_db.init_budget(20)
    prio_harness.prio_queue = [
        RunCallResult(dispatches=[_scout_dispatch(question_page.id, "main-phase")]),
        RunCallResult(dispatches=[]),
    ]

    orch = TwoPhaseOrchestrator(tmp_db)
    await orch.run(question_page.id)

    tasks_from_prio = [c.get("task", "") for c in prio_harness.prio_calls]
    initial_tasks = [t for t in tasks_from_prio if "fan out exploratory research" in t]
    assert initial_tasks == [], "initial_prioritization ran even though a judgement already exists"


@pytest.mark.asyncio
async def test_initial_prio_skipped_when_view_exists(tmp_db, question_page, prio_harness):
    """A pre-existing view on the scope question causes initial prio to be skipped."""
    await _seed_view(tmp_db, question_page.id)
    await tmp_db.init_budget(20)
    prio_harness.prio_queue = [
        RunCallResult(dispatches=[_scout_dispatch(question_page.id, "main-phase")]),
        RunCallResult(dispatches=[]),
    ]

    orch = TwoPhaseOrchestrator(tmp_db)
    await orch.run(question_page.id)

    tasks_from_prio = [c.get("task", "") for c in prio_harness.prio_calls]
    initial_tasks = [t for t in tasks_from_prio if "fan out exploratory research" in t]
    assert initial_tasks == []


@pytest.mark.asyncio
async def test_main_phase_proceeds_after_skipped_initial(tmp_db, question_page, prio_harness):
    """Initial-prio skip still allows main-phase to run and dispatch."""
    await _seed_judgement(tmp_db, question_page.id)
    await tmp_db.init_budget(20)
    prio_harness.prio_queue = [
        RunCallResult(dispatches=[_scout_dispatch(question_page.id, "post-skip")]),
        RunCallResult(dispatches=[]),
    ]

    orch = TwoPhaseOrchestrator(tmp_db)
    await orch.run(question_page.id)

    post_skip_scouts = [
        d
        for d in prio_harness.dispatched
        if d["call_type"] == CallType.FIND_CONSIDERATIONS.value
        and d["question_id"] == question_page.id
    ]
    assert len(post_skip_scouts) >= 1, "main phase did not dispatch after the skip"


@pytest.mark.asyncio
async def test_eagerly_created_initial_call_is_marked_complete_on_skip(
    tmp_db, question_page, prio_harness
):
    """The pre-created initial_prioritization call must be marked COMPLETE, not left PENDING."""
    await _seed_judgement(tmp_db, question_page.id)
    await tmp_db.init_budget(20)
    prio_harness.prio_queue = [
        RunCallResult(dispatches=[_scout_dispatch(question_page.id, "post-skip")]),
        RunCallResult(dispatches=[]),
    ]

    orch = TwoPhaseOrchestrator(tmp_db)
    eager_id = await orch.create_initial_call(question_page.id)
    await orch.run(question_page.id)

    refreshed = await tmp_db.get_call(eager_id)
    assert refreshed is not None
    assert refreshed.status == CallStatus.COMPLETE, (
        f"eagerly-created initial call was left in {refreshed.status}, expected COMPLETE"
    )
