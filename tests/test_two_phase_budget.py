"""Budget invariants for ``TwoPhaseOrchestrator.run()``.

These tests pin the budget accounting guarantees most likely to silently
break when prioritization moves from "one prioritizer traversing the
graph" to "a prioritizer at every node" with subscriptions and budget
transfers. They target the public ``run()`` entry point so they survive
internal refactors.

LLM plumbing is mocked via the shared ``prio_harness`` fixture; the
orchestrator's control-flow, budget bookkeeping, and dispatch routing
all execute for real.
"""

import pytest

from rumil.calls.common import RunCallResult
from rumil.constants import MIN_TWOPHASE_BUDGET
from rumil.models import (
    CallType,
    Dispatch,
    ScoutDispatchPayload,
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


async def _count_calls_of_type(db, call_type: CallType, project_id: str) -> int:
    rows = (
        await db._execute(
            db.client.table("calls")
            .select("id")
            .eq("call_type", call_type.value)
            .eq("project_id", project_id)
        )
    ).data
    return len(rows)


@pytest.mark.asyncio
async def test_twophase_raises_when_below_min_budget(tmp_db, question_page, prio_harness):
    await tmp_db.init_budget(MIN_TWOPHASE_BUDGET - 1)
    orch = TwoPhaseOrchestrator(tmp_db)
    with pytest.raises(ValueError, match="budget"):
        await orch.run(question_page.id)


def test_twophase_effective_budget_respects_cap(tmp_db):
    """Pins the _effective_budget formula: min(global_remaining, cap - consumed)."""
    orch = TwoPhaseOrchestrator(tmp_db, budget_cap=5)
    assert orch._effective_budget(100) == 5
    orch._consumed = 3
    assert orch._effective_budget(100) == 2
    orch._consumed = 5
    assert orch._effective_budget(100) == 0
    orch._consumed = 10
    assert orch._effective_budget(100) < 0


@pytest.mark.asyncio
async def test_twophase_budget_cap_limits_rounds(tmp_db, question_page, prio_harness):
    """budget_cap bounds total dispatch rounds even when global budget is plentiful.

    With global budget = 200 but cap = 20, the loop must terminate long before
    exhausting the scripted prio queue. Consumption also stays bounded by the cap.
    """
    await tmp_db.init_budget(200)
    prio_harness.prio_queue = [
        RunCallResult(dispatches=[_scout_dispatch(question_page.id)]) for _ in range(50)
    ]

    budget_cap = 20
    orch = TwoPhaseOrchestrator(tmp_db, budget_cap=budget_cap)
    await orch.run(question_page.id)

    assert orch._consumed <= budget_cap, f"_consumed={orch._consumed} exceeded cap={budget_cap}"
    assert len(prio_harness.prio_queue) > 0, (
        "prio queue was fully drained — budget_cap did not bound the number of rounds"
    )


@pytest.mark.asyncio
async def test_twophase_stops_loop_when_budget_hits_zero(tmp_db, question_page, prio_harness):
    """When every prio plans many dispatches but budget is tight, the loop still terminates."""
    await tmp_db.init_budget(5)
    prio_harness.prio_queue = [
        RunCallResult(dispatches=[_scout_dispatch(question_page.id) for _ in range(15)]),
        RunCallResult(dispatches=[_scout_dispatch(question_page.id) for _ in range(15)]),
    ]

    orch = TwoPhaseOrchestrator(tmp_db)
    await orch.run(question_page.id)

    scout_calls = await _count_calls_of_type(
        tmp_db, CallType.FIND_CONSIDERATIONS, tmp_db.project_id
    )
    assert scout_calls <= 5

    prio_calls = await _count_calls_of_type(tmp_db, CallType.PRIORITIZATION, tmp_db.project_id)
    assert prio_calls <= 2, f"prioritization loop ran {prio_calls} times — should terminate quickly"


@pytest.mark.asyncio
async def test_twophase_budget_conservation(tmp_db, question_page, prio_harness):
    """After run(), total/used bookkeeping stays consistent (total - used == remaining, ≥ 0)."""
    await tmp_db.init_budget(20)
    prio_harness.prio_queue = [
        RunCallResult(dispatches=[_scout_dispatch(question_page.id) for _ in range(3)]),
        RunCallResult(dispatches=[_scout_dispatch(question_page.id) for _ in range(3)]),
        RunCallResult(dispatches=[]),
    ]

    orch = TwoPhaseOrchestrator(tmp_db)
    await orch.run(question_page.id)

    total, used = await tmp_db.get_budget()
    remaining = await tmp_db.budget_remaining()
    assert total - used == remaining
    assert remaining >= 0
    assert used >= 0
    assert used <= total
