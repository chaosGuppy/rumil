"""Tests for the orchestrator's dispatch execution loop."""

import uuid

import pytest

from rumil.database import DB
from rumil.models import (
    AssessDispatchPayload,
    CallType,
    Dispatch,
    FindConsiderationsMode,
    ScoutDispatchPayload,
)
from rumil.orchestrator import (
    BaseOrchestrator,
    PrioritizationResult,
    TwoPhaseOrchestrator,
)
from rumil.tracing.tracer import CallTrace


class ScriptedOrchestrator(BaseOrchestrator):
    """Returns pre-scripted batches of dispatches, one per loop iteration."""

    def __init__(self, db, batches, call_id=None):
        super().__init__(db)
        self._batches = list(batches)
        self._index = 0
        self._call_id = call_id
        self.get_calls_count = 0

    async def run(self, root_question_id):
        await self._setup()
        try:
            for batch in self._batches:
                remaining = await self.db.budget_remaining()
                if remaining <= 0:
                    break
                self.get_calls_count += 1
                if not batch:
                    break
                await self._run_sequences(
                    [batch],
                    root_question_id,
                    self._call_id,
                )
        finally:
            await self._teardown()


def _scout_dispatch(question_id: str, **kwargs) -> Dispatch:
    kwargs.setdefault("mode", FindConsiderationsMode.ALTERNATE)
    return Dispatch(
        call_type=CallType.FIND_CONSIDERATIONS,
        payload=ScoutDispatchPayload(question_id=question_id, **kwargs),
    )


def _assess_dispatch(question_id: str, **kwargs) -> Dispatch:
    return Dispatch(
        call_type=CallType.ASSESS,
        payload=AssessDispatchPayload(question_id=question_id, **kwargs),
    )


@pytest.mark.integration
async def test_scout_dispatch_creates_scout_call(tmp_db, question_page):
    """A scout dispatch should produce a scout call in the DB."""
    orch = ScriptedOrchestrator(
        tmp_db,
        batches=[
            [_scout_dispatch(question_page.id, max_rounds=1)],
        ],
    )
    await orch.run(question_page.id)

    rows = (
        await tmp_db.client.table("calls")
        .select("call_type")
        .eq("run_id", tmp_db.run_id)
        .eq("call_type", "find_considerations")
        .execute()
    )
    assert len(rows.data) >= 1


@pytest.mark.integration
async def test_assess_dispatch_creates_assess_call(tmp_db, question_page):
    """An assess dispatch should produce an assess call in the DB."""
    orch = ScriptedOrchestrator(
        tmp_db,
        batches=[
            [_assess_dispatch(question_page.id)],
        ],
    )
    await orch.run(question_page.id)

    rows = (
        await tmp_db.client.table("calls")
        .select("call_type")
        .eq("run_id", tmp_db.run_id)
        .eq("call_type", "assess")
        .execute()
    )
    assert len(rows.data) >= 1


@pytest.mark.integration
async def test_budget_exhaustion_limits_dispatches(tmp_db, question_page):
    """Only dispatches that fit within the budget should execute."""
    await tmp_db.init_budget(1)
    orch = ScriptedOrchestrator(
        tmp_db,
        batches=[
            [_scout_dispatch(question_page.id, max_rounds=1)],
            [_scout_dispatch(question_page.id, max_rounds=1)],
            [_scout_dispatch(question_page.id, max_rounds=1)],
        ],
    )
    await orch.run(question_page.id)

    rows = (
        await tmp_db.client.table("calls")
        .select("call_type")
        .eq("run_id", tmp_db.run_id)
        .eq("call_type", "find_considerations")
        .execute()
    )
    assert len(rows.data) == 1


async def test_empty_dispatches_exits_loop(tmp_db, question_page):
    """When the orchestrator has no dispatches, the loop should exit."""
    orch = ScriptedOrchestrator(tmp_db, batches=[])
    await orch.run(question_page.id)

    rows = (
        await tmp_db.client.table("calls")
        .select("call_type")
        .eq("run_id", tmp_db.run_id)
        .execute()
    )
    call_types = {r["call_type"] for r in rows.data}
    assert "find_considerations" not in call_types
    assert "assess" not in call_types


@pytest.mark.integration
async def test_reprioritization_on_leftover_budget(tmp_db, question_page):
    """Orchestrator should process multiple batches when budget remains."""
    await tmp_db.init_budget(5)
    orch = ScriptedOrchestrator(
        tmp_db,
        batches=[
            [_scout_dispatch(question_page.id, max_rounds=1)],
            [_assess_dispatch(question_page.id)],
        ],
    )
    await orch.run(question_page.id)

    assert orch.get_calls_count >= 2


async def test_no_infinite_loop_when_nothing_spent(tmp_db, question_page):
    """If budget is 0 the loop should exit immediately, not spin."""
    await tmp_db.init_budget(0)
    orch = ScriptedOrchestrator(
        tmp_db,
        batches=[
            [_scout_dispatch(question_page.id, max_rounds=1)],
        ],
    )
    await orch.run(question_page.id)

    assert orch.get_calls_count == 0


@pytest.mark.integration
async def test_unresolvable_question_id_falls_back_to_root(tmp_db, question_page):
    """When a dispatch references a non-existent page, the root question is used."""
    fake_id = str(uuid.uuid4())
    orch = ScriptedOrchestrator(
        tmp_db,
        batches=[
            [_assess_dispatch(fake_id)],
        ],
    )
    await orch.run(question_page.id)

    rows = (
        await tmp_db.client.table("calls")
        .select("scope_page_id")
        .eq("run_id", tmp_db.run_id)
        .eq("call_type", "assess")
        .execute()
    )
    assert len(rows.data) == 1
    assert rows.data[0]["scope_page_id"] == question_page.id


@pytest.mark.integration
async def test_dispatch_executed_events_recorded(tmp_db, question_page):
    """DispatchExecutedEvent should be persisted to trace_json."""
    p_call = await tmp_db.create_call(
        CallType.PRIORITIZATION,
        scope_page_id=question_page.id,
    )
    trace = CallTrace(p_call.id, tmp_db)

    orch = ScriptedOrchestrator(
        tmp_db,
        batches=[[_assess_dispatch(question_page.id)]],
        call_id=p_call.id,
    )
    await orch.run(question_page.id)

    rows = (
        await tmp_db.client.table("calls")
        .select("trace_json")
        .eq("id", p_call.id)
        .execute()
    )
    trace_json = rows.data[0]["trace_json"]
    dispatch_events = [e for e in trace_json if e.get("event") == "dispatch_executed"]
    assert len(dispatch_events) >= 1
    evt = dispatch_events[0]
    assert evt["index"] == 0
    assert evt["child_call_type"] == "assess"
    assert evt["question_id"] == question_page.id
    assert evt["child_call_id"] is not None


async def test_concurrent_dispatch_failure_recorded_in_trace(
    tmp_db, question_page, mocker,
):
    """When a dispatch raises during the TwoPhaseOrchestrator's concurrent
    gather, an ErrorEvent should be recorded on the prioritization call's trace.
    """
    p_call = await tmp_db.create_call(
        CallType.PRIORITIZATION,
        scope_page_id=question_page.id,
    )

    orch = TwoPhaseOrchestrator(tmp_db)
    orch._call_id = p_call.id

    dispatches = [[_assess_dispatch(question_page.id)]]
    call_count = 0

    async def fake_get_next_batch(question_id, budget, parent_call_id=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return PrioritizationResult(
                dispatch_sequences=dispatches,
                call_id=p_call.id,
            )
        return PrioritizationResult(dispatch_sequences=[])

    mocker.patch.object(orch, "_get_next_batch", side_effect=fake_get_next_batch)
    mocker.patch.object(
        DB, "resolve_page_id",
        side_effect=ConnectionError("Simulated connection failure"),
    )

    await orch.run(question_page.id)

    rows = (
        await tmp_db.client.table("calls")
        .select("trace_json")
        .eq("id", p_call.id)
        .execute()
    )
    trace_json = rows.data[0]["trace_json"]
    error_events = [e for e in trace_json if e.get("event") == "error"]
    assert len(error_events) >= 1
    assert "Simulated connection failure" in error_events[0]["message"]
