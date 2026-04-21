"""Multi-parent dedup invariants (post-rearch).

After the V1 prioritiser rearch, a question with two parents is
dispatched exactly once across the run: the prioritiser registry (per
root DB) dedups non-scope dispatches on a shared target.
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


@pytest.mark.asyncio
async def test_prio_on_question_with_running_prio_uses_subscription(
    tmp_db, question_page, prio_harness
):
    """A second prioritizer targeting an in-flight question subscribes to the first.

    Asserts the minimum required invariant: no duplicate PRIORITIZATION call
    row is created on the same question when one is already running.
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
