"""Tests for the orchestrator's dispatch execution loop and prioritizer integration."""

import uuid

import pytest

from rumil.models import (
    AssessDispatchPayload,
    CallType,
    Dispatch,
    ScoutDispatchPayload,
)
from rumil.orchestrator import Orchestrator
from rumil.prioritizer import Prioritizer, PrioritizationResult
from rumil.tracing.tracer import CallTrace


class ScriptedPrioritizer(Prioritizer):
    """Returns pre-scripted batches of dispatches, one per get_calls() invocation."""

    def __init__(self, batches: list[list[Dispatch]], call_id: str | None = None, trace: CallTrace | None = None):
        self._batches = list(batches)
        self._index = 0
        self._call_id = call_id
        self._trace = trace
        self.get_calls_count = 0

    async def get_calls(
        self,
        question_id: str,
        budget: int,
        parent_call_id: str | None = None,
    ) -> PrioritizationResult:
        self.get_calls_count += 1
        if self._index < len(self._batches):
            batch = self._batches[self._index]
            self._index += 1
            return PrioritizationResult(
                dispatches=batch,
                call_id=self._call_id,
                trace=self._trace,
            )
        return PrioritizationResult(dispatches=[])

    def mark_executed(self) -> None:
        pass


def _scout_dispatch(question_id: str, **kwargs) -> Dispatch:
    return Dispatch(
        call_type=CallType.SCOUT,
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
    prioritizer = ScriptedPrioritizer([
        [_scout_dispatch(question_page.id, max_rounds=1)],
    ])
    orch = Orchestrator(tmp_db, prioritizer=prioritizer)
    await orch.run(question_page.id)

    rows = (
        await tmp_db.client.table("calls")
        .select("call_type")
        .eq("run_id", tmp_db.run_id)
        .eq("call_type", "scout")
        .execute()
    )
    assert len(rows.data) >= 1


@pytest.mark.integration
async def test_assess_dispatch_creates_assess_call(tmp_db, question_page):
    """An assess dispatch should produce an assess call in the DB."""
    prioritizer = ScriptedPrioritizer([
        [_assess_dispatch(question_page.id)],
    ])
    orch = Orchestrator(tmp_db, prioritizer=prioritizer)
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
    prioritizer = ScriptedPrioritizer([
        [
            _scout_dispatch(question_page.id, max_rounds=1),
            _scout_dispatch(question_page.id, max_rounds=1),
            _scout_dispatch(question_page.id, max_rounds=1),
        ],
    ])
    orch = Orchestrator(tmp_db, prioritizer=prioritizer)
    await orch.run(question_page.id)

    rows = (
        await tmp_db.client.table("calls")
        .select("call_type")
        .eq("run_id", tmp_db.run_id)
        .eq("call_type", "scout")
        .execute()
    )
    assert len(rows.data) == 1


async def test_empty_dispatches_exits_loop(tmp_db, question_page):
    """When the prioritizer returns no dispatches, the loop should exit."""
    prioritizer = ScriptedPrioritizer([])
    orch = Orchestrator(tmp_db, prioritizer=prioritizer)
    await orch.run(question_page.id)

    rows = (
        await tmp_db.client.table("calls")
        .select("call_type")
        .eq("run_id", tmp_db.run_id)
        .execute()
    )
    call_types = {r["call_type"] for r in rows.data}
    assert "scout" not in call_types
    assert "assess" not in call_types


@pytest.mark.integration
async def test_reprioritization_on_leftover_budget(tmp_db, question_page):
    """Prioritizer should be queried again when budget remains after execution."""
    await tmp_db.init_budget(5)
    prioritizer = ScriptedPrioritizer([
        [_scout_dispatch(question_page.id, max_rounds=1)],
        [_assess_dispatch(question_page.id)],
    ])
    orch = Orchestrator(tmp_db, prioritizer=prioritizer)
    await orch.run(question_page.id)

    assert prioritizer.get_calls_count >= 2


async def test_no_infinite_loop_when_nothing_spent(tmp_db, question_page):
    """If budget is 0 the loop should exit immediately, not spin."""
    await tmp_db.init_budget(0)
    prioritizer = ScriptedPrioritizer([
        [_scout_dispatch(question_page.id, max_rounds=1)],
    ])
    orch = Orchestrator(tmp_db, prioritizer=prioritizer)
    await orch.run(question_page.id)

    assert prioritizer.get_calls_count == 0


@pytest.mark.integration
async def test_unresolvable_question_id_falls_back_to_root(tmp_db, question_page):
    """When a dispatch references a non-existent page, the root question is used."""
    fake_id = str(uuid.uuid4())
    prioritizer = ScriptedPrioritizer([
        [_assess_dispatch(fake_id)],
    ])
    orch = Orchestrator(tmp_db, prioritizer=prioritizer)
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

    prioritizer = ScriptedPrioritizer(
        batches=[[_assess_dispatch(question_page.id)]],
        call_id=p_call.id,
        trace=trace,
    )
    orch = Orchestrator(tmp_db, prioritizer=prioritizer)
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
