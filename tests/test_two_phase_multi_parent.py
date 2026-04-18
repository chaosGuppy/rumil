"""Multi-parent behaviour pins.

Today, a question with two parents gets dispatched twice when each
parent's prioritizer independently targets it — there is no
subscription or dedup layer. The prioritizer rearch will introduce
subscriptions so that one investigation can reuse another's result.

These tests split into two groups:
* **Current behaviour (non-xfail)** — pins duplicate dispatch so we
  notice if it silently changes.
* **Rearch target (xfail)** — specifies the post-rearch semantics.
  When the rearch lands, these should start passing; the rearch
  author should flip ``strict=False`` → ``strict=True``.
"""

import pytest

from rumil.calls.common import RunCallResult
from rumil.models import (
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


async def _make_question(db, headline: str) -> Page:
    page = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=headline,
        headline=headline,
    )
    await db.save_page(page)
    return page


async def _link_child(db, parent: Page, child: Page) -> None:
    await db.save_link(
        PageLink(
            from_page_id=parent.id,
            to_page_id=child.id,
            link_type=LinkType.CHILD_QUESTION,
        )
    )


@pytest.mark.asyncio
async def test_multi_parent_child_gets_dispatched_twice_today(tmp_db, prio_harness):
    """Two parents each prioritizing the same child → two scout calls on that child.

    This pins *current* behavior. When the rearch introduces subscriptions,
    this test will start failing — at which point the rearch author should
    delete this test and un-xfail the sibling target-behavior test.
    """
    p1 = await _make_question(tmp_db, "Parent one.")
    p2 = await _make_question(tmp_db, "Parent two.")
    child = await _make_question(tmp_db, "Shared child.")
    await _link_child(tmp_db, p1, child)
    await _link_child(tmp_db, p2, child)

    await tmp_db.init_budget(30)
    prio_harness.prio_queue = [
        RunCallResult(dispatches=[_scout_dispatch(p1.id, "seed p1")]),
        RunCallResult(dispatches=[_scout_dispatch(child.id, "p1 targets child")]),
        RunCallResult(dispatches=[]),
        RunCallResult(dispatches=[_scout_dispatch(p2.id, "seed p2")]),
        RunCallResult(dispatches=[_scout_dispatch(child.id, "p2 targets child")]),
        RunCallResult(dispatches=[]),
    ]

    orch_p1 = TwoPhaseOrchestrator(tmp_db)
    await orch_p1.run(p1.id)
    orch_p2 = TwoPhaseOrchestrator(tmp_db)
    await orch_p2.run(p2.id)

    child_scouts = [
        d
        for d in prio_harness.dispatched
        if d["question_id"] == child.id and d["call_type"] == CallType.FIND_CONSIDERATIONS.value
    ]
    assert len(child_scouts) >= 2, (
        f"expected ≥2 scout dispatches on shared child (today's behavior), got {len(child_scouts)}"
    )


@pytest.mark.xfail(
    reason="rearch target: subscriptions should dedup cross-parent dispatches",
    strict=False,
)
@pytest.mark.asyncio
async def test_multi_parent_dedup_across_parents(tmp_db, prio_harness):
    """Post-rearch target: a shared child should be investigated exactly once across parents.

    Both parents' prioritizers should observe the single investigation's
    result via a subscription rather than each dispatching their own.
    """
    p1 = await _make_question(tmp_db, "Parent one.")
    p2 = await _make_question(tmp_db, "Parent two.")
    child = await _make_question(tmp_db, "Shared child.")
    await _link_child(tmp_db, p1, child)
    await _link_child(tmp_db, p2, child)

    await tmp_db.init_budget(30)
    prio_harness.prio_queue = [
        RunCallResult(dispatches=[_scout_dispatch(p1.id, "seed p1")]),
        RunCallResult(dispatches=[_scout_dispatch(child.id, "p1 targets child")]),
        RunCallResult(dispatches=[]),
        RunCallResult(dispatches=[_scout_dispatch(p2.id, "seed p2")]),
        RunCallResult(dispatches=[_scout_dispatch(child.id, "p2 targets child")]),
        RunCallResult(dispatches=[]),
    ]

    orch_p1 = TwoPhaseOrchestrator(tmp_db)
    await orch_p1.run(p1.id)
    orch_p2 = TwoPhaseOrchestrator(tmp_db)
    await orch_p2.run(p2.id)

    child_scouts = [
        d
        for d in prio_harness.dispatched
        if d["question_id"] == child.id and d["call_type"] == CallType.FIND_CONSIDERATIONS.value
    ]
    assert len(child_scouts) == 1, (
        f"post-rearch: expected single scout on shared child, got {len(child_scouts)}"
    )


@pytest.mark.xfail(
    reason="rearch target: concurrent prio on the same question should subscribe, not duplicate",
    strict=False,
)
@pytest.mark.asyncio
async def test_prio_on_question_with_running_prio_uses_subscription(
    tmp_db, question_page, prio_harness
):
    """Post-rearch: a second prioritizer targeting an in-flight question subscribes to the first.

    Exact subscription shape is TBD by the rearch. This placeholder asserts
    the minimum required invariant: no duplicate PRIORITIZATION call row is
    created on the same question when one is already running.
    """
    await tmp_db.init_budget(30)
    prio_harness.prio_queue = [
        RunCallResult(dispatches=[_scout_dispatch(question_page.id, "seed")]),
        RunCallResult(dispatches=[]),
        RunCallResult(dispatches=[_scout_dispatch(question_page.id, "seed")]),
        RunCallResult(dispatches=[]),
    ]

    orch_a = TwoPhaseOrchestrator(tmp_db)
    await orch_a.run(question_page.id)
    orch_b = TwoPhaseOrchestrator(tmp_db)
    await orch_b.run(question_page.id)

    rows = (
        await tmp_db._execute(
            tmp_db.client.table("calls")
            .select("id")
            .eq("call_type", CallType.PRIORITIZATION.value)
            .eq("scope_page_id", question_page.id)
            .eq("project_id", tmp_db.project_id)
        )
    ).data
    assert len(rows) <= 2, (
        f"post-rearch: expected ≤2 prio call rows on the same question (one per run), got {len(rows)}"
    )
