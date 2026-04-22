"""Tests for the per-question shared budget pool.

Covers the DB-level RPC behaviour (register/consume/unregister/recurse),
the in-flight call queries used by build_prioritization_context, the
context section rendered for prio prompts, and orchestrator-level
behaviour (registration, loop stop, recurse handoff).
"""

import asyncio

import pytest

from rumil.calls.common import RunCallResult
from rumil.constants import MIN_TWOPHASE_BUDGET
from rumil.context import build_prioritization_context
from rumil.models import (
    Call,
    CallStatus,
    CallType,
    Dispatch,
    LinkType,
    Page,
    PageLayer,
    PageLink,
    PageType,
    RecurseDispatchPayload,
    ScoutDispatchPayload,
    Workspace,
)
from rumil.orchestrators.two_phase import TwoPhaseOrchestrator


def _scout_dispatch(question_id: str, reason: str = "seed") -> Dispatch:
    return Dispatch(
        call_type=CallType.FIND_CONSIDERATIONS,
        payload=ScoutDispatchPayload(
            question_id=question_id,
            max_rounds=1,
            reason=reason,
        ),
    )


@pytest.mark.asyncio
async def test_register_increments_contributed_and_active_calls(tmp_db, question_page):
    """Two register calls sum contributions and increment active_calls."""
    pool = await tmp_db.qbp_get(question_page.id)
    assert pool.contributed == 0
    assert pool.active_calls == 0

    await tmp_db.qbp_register(question_page.id, 10)
    pool = await tmp_db.qbp_get(question_page.id)
    assert pool.contributed == 10
    assert pool.active_calls == 1

    await tmp_db.qbp_register(question_page.id, 7)
    pool = await tmp_db.qbp_get(question_page.id)
    assert pool.contributed == 17
    assert pool.active_calls == 2


@pytest.mark.asyncio
async def test_consume_drains_pool(tmp_db, question_page):
    """Consume reports remaining and exhaustion accurately."""
    await tmp_db.qbp_register(question_page.id, 10)

    remaining, exhausted = await tmp_db.qbp_consume(question_page.id, 7)
    assert remaining == 3
    assert exhausted is False

    remaining, exhausted = await tmp_db.qbp_consume(question_page.id, 3)
    assert remaining == 0
    assert exhausted is True

    remaining, exhausted = await tmp_db.qbp_consume(question_page.id, 1)
    assert remaining == -1
    assert exhausted is True


@pytest.mark.asyncio
async def test_unregister_decrements_active_calls_only(tmp_db, question_page):
    """Unregister reduces active_calls but leaves contributed/consumed intact."""
    await tmp_db.qbp_register(question_page.id, 5)
    await tmp_db.qbp_consume(question_page.id, 2)

    await tmp_db.qbp_unregister(question_page.id)
    pool = await tmp_db.qbp_get(question_page.id)
    assert pool.contributed == 5
    assert pool.consumed == 2
    assert pool.active_calls == 0


@pytest.mark.asyncio
async def test_unregister_floors_at_zero(tmp_db, question_page):
    """Extra unregisters don't push active_calls negative."""
    await tmp_db.qbp_register(question_page.id, 3)
    await tmp_db.qbp_unregister(question_page.id)
    await tmp_db.qbp_unregister(question_page.id)
    pool = await tmp_db.qbp_get(question_page.id)
    assert pool.active_calls == 0


@pytest.mark.asyncio
async def test_consume_unknown_pool_returns_sentinel(tmp_db, question_page):
    """Consuming a non-existent pool reports 'never exhausted' and doesn't error."""
    remaining, exhausted = await tmp_db.qbp_consume(question_page.id, 5)
    assert exhausted is False
    assert remaining > 0


@pytest.mark.asyncio
async def test_qbp_get_many_returns_only_existing(tmp_db, question_page, child_question_page):
    """Batched get returns one entry per existing pool, missing IDs absent."""
    await tmp_db.qbp_register(question_page.id, 5)
    pools = await tmp_db.qbp_get_many([question_page.id, child_question_page.id])
    assert question_page.id in pools
    assert child_question_page.id not in pools
    assert pools[question_page.id].contributed == 5


@pytest.mark.asyncio
async def test_recurse_charges_parent_and_registers_child(
    tmp_db, question_page, child_question_page
):
    """qbp_recurse atomically debits parent and registers child."""
    await tmp_db.qbp_register(question_page.id, 20)

    await tmp_db.qbp_recurse(question_page.id, child_question_page.id, 5)

    parent_pool = await tmp_db.qbp_get(question_page.id)
    child_pool = await tmp_db.qbp_get(child_question_page.id)
    assert parent_pool.consumed == 5
    assert child_pool.contributed == 5
    assert child_pool.active_calls == 1


@pytest.mark.asyncio
async def test_get_active_calls_returns_pending_and_running_excludes_self(tmp_db, question_page):
    """Active call query returns pending+running, filters by exclude_call_id."""
    pending_call = Call(
        call_type=CallType.PRIORITIZATION,
        workspace=Workspace.PRIORITIZATION,
        scope_page_id=question_page.id,
        status=CallStatus.PENDING,
        budget_allocated=12,
    )
    running_call = Call(
        call_type=CallType.FIND_CONSIDERATIONS,
        workspace=Workspace.RESEARCH,
        scope_page_id=question_page.id,
        status=CallStatus.RUNNING,
        budget_allocated=4,
    )
    completed_call = Call(
        call_type=CallType.ASSESS,
        workspace=Workspace.RESEARCH,
        scope_page_id=question_page.id,
        status=CallStatus.COMPLETE,
    )
    self_call = Call(
        call_type=CallType.PRIORITIZATION,
        workspace=Workspace.PRIORITIZATION,
        scope_page_id=question_page.id,
        status=CallStatus.RUNNING,
    )
    for c in (pending_call, running_call, completed_call, self_call):
        await tmp_db.save_call(c)

    active = await tmp_db.get_active_calls_for_question(
        question_page.id,
        exclude_call_id=self_call.id,
    )
    active_ids = {c.id for c in active}
    assert pending_call.id in active_ids
    assert running_call.id in active_ids
    assert completed_call.id not in active_ids
    assert self_call.id not in active_ids


@pytest.mark.asyncio
async def test_get_active_prio_pools_for_subquestions(tmp_db, question_page, child_question_page):
    """Returns only subquestions whose pool has active_calls > 0."""
    other_child = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="Other child",
        headline="Other child",
    )
    await tmp_db.save_page(other_child)
    await tmp_db.save_link(
        PageLink(
            from_page_id=question_page.id,
            to_page_id=other_child.id,
            link_type=LinkType.CHILD_QUESTION,
        )
    )

    await tmp_db.qbp_register(child_question_page.id, 8)
    # other_child has no pool registered

    pools = await tmp_db.get_active_prio_pools_for_subquestions(question_page.id)
    pool_ids = {sub_id for sub_id, _ in pools}
    assert child_question_page.id in pool_ids
    assert other_child.id not in pool_ids
    sub_pool = next(p for sub_id, p in pools if sub_id == child_question_page.id)
    assert sub_pool.contributed == 8


@pytest.mark.asyncio
async def test_concurrent_consume_no_overcount(tmp_db, question_page):
    """50 concurrent consumes drain the pool exactly (FOR UPDATE regression)."""
    await tmp_db.qbp_register(question_page.id, 100)

    await asyncio.gather(*(tmp_db.qbp_consume(question_page.id, 1) for _ in range(50)))
    pool = await tmp_db.qbp_get(question_page.id)
    assert pool.consumed == 50


@pytest.mark.asyncio
async def test_orchestrator_registers_on_run_start_and_unregisters_after(
    tmp_db, question_page, prio_harness
):
    """TwoPhaseOrchestrator registers contribution at run start, unregisters in finally."""
    prio_harness.prio_queue = [
        RunCallResult(dispatches=[]),
        RunCallResult(dispatches=[]),
    ]

    parent = TwoPhaseOrchestrator(tmp_db, budget_cap=15)
    await parent.run(question_page.id)

    pool = await tmp_db.qbp_get(question_page.id)
    assert pool.contributed == 15
    assert pool.active_calls == 0


@pytest.mark.asyncio
async def test_context_renders_coordination_section_when_active_work_present(
    tmp_db, question_page, child_question_page
):
    """Coordination section appears when in-flight calls or sub-pools exist."""
    await tmp_db.qbp_register(question_page.id, 20)
    await tmp_db.qbp_consume(question_page.id, 3)

    in_flight = Call(
        call_type=CallType.FIND_CONSIDERATIONS,
        workspace=Workspace.RESEARCH,
        scope_page_id=question_page.id,
        status=CallStatus.RUNNING,
        budget_allocated=4,
    )
    await tmp_db.save_call(in_flight)

    await tmp_db.qbp_register(child_question_page.id, 6)

    context_text, _ = await build_prioritization_context(
        tmp_db,
        scope_question_id=question_page.id,
    )
    assert "Coordination" in context_text
    assert in_flight.id[:8] in context_text
    assert child_question_page.id[:8] in context_text
    assert "6 budget remaining" in context_text
    # Pool stats line is intentionally not duplicated in the Coordination
    # section; the orchestrator's budget line is the authoritative number.
    assert "Shared question pool" not in context_text
    assert "20 contributed" not in context_text


@pytest.mark.asyncio
async def test_context_omits_coordination_when_nothing_in_flight(tmp_db, question_page):
    """No active peer calls and no sub-pools → no Coordination section."""
    context_text, _ = await build_prioritization_context(
        tmp_db,
        scope_question_id=question_page.id,
    )
    assert "Coordination" not in context_text


@pytest.mark.asyncio
async def test_context_excludes_current_call_from_in_flight_list(tmp_db, question_page):
    """current_call_id filters self out so the prio doesn't see its own call as a peer."""
    own_call = Call(
        call_type=CallType.PRIORITIZATION,
        workspace=Workspace.PRIORITIZATION,
        scope_page_id=question_page.id,
        status=CallStatus.RUNNING,
        budget_allocated=10,
    )
    peer_call = Call(
        call_type=CallType.PRIORITIZATION,
        workspace=Workspace.PRIORITIZATION,
        scope_page_id=question_page.id,
        status=CallStatus.RUNNING,
        budget_allocated=4,
    )
    for c in (own_call, peer_call):
        await tmp_db.save_call(c)

    context_text, _ = await build_prioritization_context(
        tmp_db,
        scope_question_id=question_page.id,
        current_call_id=own_call.id,
    )
    assert peer_call.id[:8] in context_text
    assert own_call.id[:8] not in context_text


@pytest.mark.asyncio
async def test_recurse_dispatch_charges_parent_pool_and_registers_child(
    tmp_db, question_page, child_question_page, prio_harness
):
    """A RecurseDispatchPayload debits the parent pool by the recurse budget."""
    await tmp_db.init_budget(40)
    recurse = Dispatch(
        call_type=CallType.PRIORITIZATION,
        payload=RecurseDispatchPayload(
            question_id=child_question_page.id,
            budget=MIN_TWOPHASE_BUDGET,
            reason="drill into subquestion",
        ),
    )
    prio_harness.prio_queue = [
        RunCallResult(dispatches=[_scout_dispatch(question_page.id, "seed")]),
        RunCallResult(dispatches=[recurse]),
        RunCallResult(dispatches=[]),
        RunCallResult(dispatches=[]),
        RunCallResult(dispatches=[]),
    ]

    parent = TwoPhaseOrchestrator(tmp_db, budget_cap=20)
    await parent.run(question_page.id)

    parent_pool = await tmp_db.qbp_get(question_page.id)
    # The seed scout consumes 1; the recurse charges MIN_TWOPHASE_BUDGET more.
    # The child orchestrator also runs and consumes from the *child* pool.
    assert parent_pool.consumed >= 1 + MIN_TWOPHASE_BUDGET
    child_pool = await tmp_db.qbp_get(child_question_page.id)
    # Child orchestrator registered itself with budget=MIN_TWOPHASE_BUDGET when
    # its run() began.
    assert child_pool.contributed >= MIN_TWOPHASE_BUDGET


@pytest.mark.asyncio
async def test_loop_stops_when_pool_exhausted_even_if_local_budget_remains(
    tmp_db, question_page, prio_harness
):
    """A peer that drains the pool causes our orchestrator to exit early."""
    await tmp_db.init_budget(50)

    # Pre-register a peer contribution that's already entirely consumed —
    # simulating "another cycle ran first and used up the shared pool".
    await tmp_db.qbp_register(question_page.id, 10)
    await tmp_db.qbp_consume(question_page.id, 10)

    # Endless dispatch script — we expect to exit before consuming much of
    # our own budget_cap.
    prio_harness.prio_queue = [
        RunCallResult(dispatches=[_scout_dispatch(question_page.id, f"r{i}")]) for i in range(20)
    ]

    parent = TwoPhaseOrchestrator(tmp_db, budget_cap=20)
    await parent.run(question_page.id)

    # The orchestrator should have exited because pool.remaining hit 0,
    # not because budget_cap (20) was exhausted. Allow for a small bounded
    # number of consumes from rounds that fired before the loop check tripped.
    assert parent._consumed < 20

    # Pool active_calls returned to 0 after our orchestrator unregistered
    # itself (we never unregistered the simulated peer).
    pool = await tmp_db.qbp_get(question_page.id)
    assert pool.active_calls == 1
