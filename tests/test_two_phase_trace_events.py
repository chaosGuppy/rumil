"""Trace event shape & ordering invariants.

The frontend reads these events directly (via the typed API envelope),
so their shape and relative ordering is downstream-visible. When the
prioritizer rearch reshapes the event stream, these tests make the
impact explicit instead of silent.
"""

import pytest

from rumil.calls.common import RunCallResult
from rumil.models import (
    CallType,
    Dispatch,
    RecurseDispatchPayload,
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


async def _trace_events(db, call_id: str) -> list[dict]:
    rows = (await db._execute(db.client.table("calls").select("trace_json").eq("id", call_id))).data
    if not rows:
        return []
    return list(rows[0].get("trace_json") or [])


async def _prio_call_ids(db, project_id: str) -> list[str]:
    rows = (
        await db._execute(
            db.client.table("calls")
            .select("id, created_at")
            .eq("call_type", CallType.PRIORITIZATION.value)
            .eq("project_id", project_id)
            .order("created_at")
        )
    ).data
    return [r["id"] for r in rows]


@pytest.mark.asyncio
async def test_initial_prio_trace_sequence(tmp_db, question_page, prio_harness):
    """Initial prioritization trace must open with context_built, then dispatches_planned."""
    await tmp_db.init_budget(20)
    scouts = [_scout_dispatch(question_page.id, f"s{i}") for i in range(3)]
    prio_harness.prio_queue = [
        RunCallResult(dispatches=scouts),
        RunCallResult(dispatches=[]),
    ]

    orch = TwoPhaseOrchestrator(tmp_db)
    await orch.run(question_page.id)

    prio_ids = await _prio_call_ids(tmp_db, tmp_db.project_id)
    assert prio_ids, "no prioritization call rows were created"
    events = await _trace_events(tmp_db, prio_ids[0])
    kinds = [e.get("event") for e in events]
    assert "context_built" in kinds
    assert "dispatches_planned" in kinds
    assert kinds.index("context_built") < kinds.index("dispatches_planned")

    planned = next(e for e in events if e.get("event") == "dispatches_planned")
    assert len(planned.get("dispatches", [])) == 3


@pytest.mark.asyncio
async def test_main_phase_prio_trace_includes_scoring(tmp_db, question_page, prio_harness):
    """Main-phase trace must include scoring_completed with both score arrays present."""
    await tmp_db.init_budget(20)
    prio_harness.prio_queue = [
        RunCallResult(dispatches=[_scout_dispatch(question_page.id, "seed")]),
        RunCallResult(dispatches=[_scout_dispatch(question_page.id, "main")]),
        RunCallResult(dispatches=[]),
    ]

    orch = TwoPhaseOrchestrator(tmp_db)
    await orch.run(question_page.id)

    prio_ids = await _prio_call_ids(tmp_db, tmp_db.project_id)
    assert len(prio_ids) >= 2, "expected at least initial + main-phase prio calls"
    main_events = await _trace_events(tmp_db, prio_ids[1])
    kinds = [e.get("event") for e in main_events]
    assert "scoring_completed" in kinds, f"main-phase trace missing scoring_completed: {kinds}"

    scoring = next(e for e in main_events if e.get("event") == "scoring_completed")
    assert "subquestion_scores" in scoring
    assert "claim_scores" in scoring


@pytest.mark.asyncio
async def test_recurse_dispatch_executed_event_has_recurse_call_type(
    tmp_db, question_page, child_question_page, prio_harness
):
    """Recurse dispatches must surface as DispatchExecutedEvent(child_call_type='recurse')."""
    await tmp_db.init_budget(30)
    recurse = Dispatch(
        call_type=CallType.PRIORITIZATION,
        payload=RecurseDispatchPayload(
            question_id=child_question_page.id,
            budget=4,
            reason="drill into subquestion",
        ),
    )
    prio_harness.prio_queue = [
        RunCallResult(dispatches=[_scout_dispatch(question_page.id, "seed")]),
        RunCallResult(dispatches=[recurse]),
        RunCallResult(dispatches=[]),
        RunCallResult(dispatches=[]),
    ]

    orch = TwoPhaseOrchestrator(tmp_db)
    await orch.run(question_page.id)

    prio_ids = await _prio_call_ids(tmp_db, tmp_db.project_id)
    assert len(prio_ids) >= 2
    main_events = await _trace_events(tmp_db, prio_ids[1])
    recurse_events = [
        e
        for e in main_events
        if e.get("event") == "dispatch_executed" and e.get("child_call_type") == "recurse"
    ]
    assert recurse_events, (
        f"no recurse DispatchExecutedEvent in main-phase trace — kinds: "
        f"{[(e.get('event'), e.get('child_call_type')) for e in main_events]}"
    )
    assert recurse_events[0].get("child_call_id") is not None
