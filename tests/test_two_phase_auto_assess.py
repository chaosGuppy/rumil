"""Auto-assess atomicity invariants for ``TwoPhaseOrchestrator``.

When main-phase prioritization dispatches a scout onto a **subquestion**
(not the scope question), the orchestrator silently appends an
``AssessDispatchPayload`` so the subquestion ends with a fresh judgement.
The orchestrator also guarantees that this trailing assess runs even if
budget runs out mid-sequence (force-consume). These invariants are easy
for a rewrite to drop; pinning them here protects against that.
"""

import pytest

from rumil.calls.common import RunCallResult
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


@pytest.mark.asyncio
async def test_subquestion_dispatch_gets_auto_assess_appended(
    tmp_db, question_page, child_question_page, prio_harness
):
    """Main-phase scout on a subquestion runs as a [scout, assess] sequence."""
    await tmp_db.init_budget(20)
    prio_harness.prio_queue = [
        RunCallResult(dispatches=[_scout_dispatch(question_page.id, "seed initial")]),
        RunCallResult(dispatches=[_scout_dispatch(child_question_page.id, "target child")]),
        RunCallResult(dispatches=[]),
    ]

    orch = TwoPhaseOrchestrator(tmp_db)
    await orch.run(question_page.id)

    sub_dispatches = [
        d for d in prio_harness.dispatched if d["question_id"] == child_question_page.id
    ]
    call_types = [d["call_type"] for d in sub_dispatches]
    assert call_types == [
        CallType.FIND_CONSIDERATIONS.value,
        CallType.ASSESS.value,
    ], f"expected [scout, assess] on subquestion, got {call_types}"

    assert sub_dispatches[0]["sequence_id"] is not None
    assert sub_dispatches[0]["sequence_id"] == sub_dispatches[1]["sequence_id"]


@pytest.mark.asyncio
async def test_scope_dispatch_does_not_get_auto_assess(tmp_db, question_page, prio_harness):
    """A scout targeting the scope question runs alone — no trailing assess."""
    await tmp_db.init_budget(20)
    prio_harness.prio_queue = [
        RunCallResult(dispatches=[_scout_dispatch(question_page.id, "seed initial")]),
        RunCallResult(dispatches=[_scout_dispatch(question_page.id, "main-phase scope scout")]),
        RunCallResult(dispatches=[]),
    ]

    orch = TwoPhaseOrchestrator(tmp_db)
    await orch.run(question_page.id)

    scope_dispatches = [d for d in prio_harness.dispatched if d["question_id"] == question_page.id]
    assess_on_scope = [d for d in scope_dispatches if d["call_type"] == CallType.ASSESS.value]
    assert assess_on_scope == [], (
        f"scope dispatches unexpectedly got an assess appended: {assess_on_scope}"
    )


@pytest.mark.asyncio
async def test_auto_assess_runs_even_when_budget_exhausts_mid_sequence(
    tmp_db, question_page, child_question_page, prio_harness
):
    """When the scout consumes the last unit, the trailing assess force-consumes and still runs.

    init_budget=13 lets initial prio drain 12 units, leaving 1 for the main-phase
    scout; the trailing assess hits budget_remaining==0 and must force-consume.
    """
    await tmp_db.init_budget(13)
    seed_scouts = [_scout_dispatch(question_page.id, f"seed {i}") for i in range(12)]
    prio_harness.prio_queue = [
        RunCallResult(dispatches=seed_scouts),
        RunCallResult(dispatches=[_scout_dispatch(child_question_page.id, "subq scout")]),
        RunCallResult(dispatches=[]),
    ]

    orch = TwoPhaseOrchestrator(tmp_db)
    await orch.run(question_page.id)

    sub_dispatches = [
        d for d in prio_harness.dispatched if d["question_id"] == child_question_page.id
    ]
    sub_call_types = [d["call_type"] for d in sub_dispatches]
    assert CallType.FIND_CONSIDERATIONS.value in sub_call_types
    assert CallType.ASSESS.value in sub_call_types, "auto-assess was skipped when budget was tight"
    assess_entries = [d for d in sub_dispatches if d["call_type"] == CallType.ASSESS.value]
    assert any(d["force"] for d in assess_entries), (
        "trailing assess did not force-consume despite exhausted budget"
    )


@pytest.mark.asyncio
async def test_force_consume_expands_global_budget(
    tmp_db, question_page, child_question_page, prio_harness
):
    """Force-consume grows ``total`` by 1 per forced unit, so we never overdraw."""
    initial_total = 13
    await tmp_db.init_budget(initial_total)
    seed_scouts = [_scout_dispatch(question_page.id, f"seed {i}") for i in range(12)]
    prio_harness.prio_queue = [
        RunCallResult(dispatches=seed_scouts),
        RunCallResult(dispatches=[_scout_dispatch(child_question_page.id, "subq scout")]),
        RunCallResult(dispatches=[]),
    ]

    orch = TwoPhaseOrchestrator(tmp_db)
    await orch.run(question_page.id)

    total, used = await tmp_db.get_budget()
    forced_count = sum(1 for d in prio_harness.dispatched if d["force"])
    assert forced_count > 0, "expected at least one force-consume in this setup"
    assert total >= initial_total + forced_count, (
        f"total={total} should have grown by ≥ {forced_count} forced units "
        f"from initial {initial_total}"
    )
    assert used <= total
