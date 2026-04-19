"""RunExecutor read + write helpers + the start/cancel/wait control plane."""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest
import pytest_asyncio

from rumil.run_executor import RunExecutor, RunStatus
from rumil.run_executor import executor as executor_module
from rumil.run_executor.executor import _ACTIVE_RUNS, _KIND_HANDLERS
from rumil.run_executor.run_state import RunEvent


@pytest_asyncio.fixture
async def run_db(tmp_db):
    await tmp_db.create_run(name="executor-transitions", question_id=None, config={})
    return tmp_db


@pytest.fixture
def fake_handler(mocker):
    """Install a fake 'orchestrator' handler for the duration of a test.

    Returns a dict the test can mutate to control handler behavior:
    ``raise_exc`` — an exception to raise; ``sleep`` — seconds to sleep;
    ``called_with`` — populated with (spec, db) on invocation.
    """
    state: dict = {"called_with": None, "raise_exc": None, "sleep": 0.0}
    original = _KIND_HANDLERS.get("orchestrator")

    async def _fake(spec, db):
        state["called_with"] = (spec, db)
        if state["sleep"]:
            await asyncio.sleep(state["sleep"])
        if state["raise_exc"] is not None:
            raise state["raise_exc"]

    _KIND_HANDLERS["orchestrator"] = _fake
    yield state
    if original is None:
        _KIND_HANDLERS.pop("orchestrator", None)
    else:
        _KIND_HANDLERS["orchestrator"] = original
    _ACTIVE_RUNS.clear()


async def test_status_returns_none_for_unknown_run(tmp_db):
    ex = RunExecutor(tmp_db)
    assert await ex.status("does-not-exist") is None


async def test_status_default_is_pending(run_db):
    ex = RunExecutor(run_db)
    view = await ex.status(run_db.run_id)
    assert view is not None
    assert view.status == RunStatus.PENDING
    assert view.started_at is None
    assert view.finished_at is None


async def test_mark_started_transitions_pending(run_db):
    ex = RunExecutor(run_db)
    await ex.mark_started(run_db.run_id)
    view = await ex.status(run_db.run_id)
    assert view is not None
    assert view.status == RunStatus.RUNNING
    assert view.started_at is not None


async def test_mark_complete_sets_finished_and_cost(run_db):
    ex = RunExecutor(run_db)
    await ex.mark_started(run_db.run_id)
    await ex.mark_complete(run_db.run_id, cost_usd_cents=1234)
    view = await ex.status(run_db.run_id)
    assert view is not None
    assert view.status == RunStatus.COMPLETE
    assert view.finished_at is not None
    assert float(view.cost_usd) == 12.34


async def test_mark_failed_records_reason(run_db):
    ex = RunExecutor(run_db)
    await ex.mark_failed(run_db.run_id, reason="exchange exploded")
    view = await ex.status(run_db.run_id)
    assert view is not None
    assert view.status == RunStatus.FAILED
    assert view.cancel_reason == "exchange exploded"


async def test_mark_cancelled_records_reason(run_db):
    ex = RunExecutor(run_db)
    await ex.mark_cancelled(run_db.run_id, reason="user pressed cancel")
    view = await ex.status(run_db.run_id)
    assert view is not None
    assert view.status == RunStatus.CANCELLED
    assert view.cancel_reason == "user pressed cancel"


async def test_mark_started_is_idempotent_only_on_pending(run_db):
    ex = RunExecutor(run_db)
    await ex.mark_started(run_db.run_id)
    first = await ex.status(run_db.run_id)
    assert first is not None and first.started_at is not None
    started_at = first.started_at

    await ex.mark_started(run_db.run_id)
    second = await ex.status(run_db.run_id)
    assert second is not None
    assert second.started_at == started_at


async def test_create_run_from_spec_creates_row_and_inits_budget(tmp_db):
    from rumil.run_executor.run_spec import RunSpec

    ex = RunExecutor(tmp_db)
    spec = RunSpec(
        kind="orchestrator",
        project_id=tmp_db.project_id,
        budget_calls=7,
        name="spec-test",
        origin="cli",
    )
    run_id = await ex.create_run_from_spec(spec)
    assert run_id == tmp_db.run_id

    view = await ex.status(run_id)
    assert view is not None
    assert view.name == "spec-test"
    assert view.status == RunStatus.PENDING
    assert view.config.get("origin") == "cli"

    total, used = await tmp_db.get_budget()
    assert total == 7
    assert used == 0


async def test_create_run_from_spec_respects_staged_consistency(tmp_db):
    import pytest

    from rumil.run_executor.run_spec import RunSpec

    ex = RunExecutor(tmp_db)
    # tmp_db is non-staged; spec.staged=True must raise.
    spec = RunSpec(
        kind="orchestrator",
        project_id=tmp_db.project_id,
        staged=True,
    )
    with pytest.raises(ValueError, match="staged=True"):
        await ex.create_run_from_spec(spec)


async def test_tracked_scope_marks_complete_on_success(run_db):
    ex = RunExecutor(run_db)
    async with ex.tracked_scope(run_db.run_id):
        pass
    view = await ex.status(run_db.run_id)
    assert view is not None
    assert view.status == RunStatus.COMPLETE
    assert view.started_at is not None
    assert view.finished_at is not None


async def test_tracked_scope_marks_failed_on_exception(run_db):
    ex = RunExecutor(run_db)
    with pytest.raises(RuntimeError, match="boom"):
        async with ex.tracked_scope(run_db.run_id):
            raise RuntimeError("boom")
    view = await ex.status(run_db.run_id)
    assert view is not None
    assert view.status == RunStatus.FAILED
    assert view.cancel_reason is not None
    assert "RuntimeError" in view.cancel_reason
    assert "boom" in view.cancel_reason


async def test_tracked_scope_marks_cancelled_on_cancellation(run_db):
    ex = RunExecutor(run_db)
    with pytest.raises(asyncio.CancelledError):
        async with ex.tracked_scope(run_db.run_id):
            raise asyncio.CancelledError()
    view = await ex.status(run_db.run_id)
    assert view is not None
    assert view.status == RunStatus.CANCELLED


async def test_start_spawns_handler_and_transitions_to_complete(tmp_db, fake_handler):
    from rumil.run_executor import RunSpec

    ex = RunExecutor(tmp_db)
    spec = RunSpec(
        kind="orchestrator",
        project_id=tmp_db.project_id,
        question_id="00000000-0000-0000-0000-000000000001",
        name="start-test",
    )
    run_id = await ex.start(spec)
    view = await ex.wait_until_settled(run_id, timeout=5.0)

    assert view is not None
    assert view.status == RunStatus.COMPLETE
    assert view.started_at is not None
    assert view.finished_at is not None
    assert fake_handler["called_with"] is not None
    called_spec, called_db = fake_handler["called_with"]
    assert called_spec.name == "start-test"
    assert called_db is tmp_db


async def test_start_refuses_second_handler_on_same_run(tmp_db, fake_handler):
    from rumil.run_executor import RunSpec

    fake_handler["sleep"] = 0.3
    ex = RunExecutor(tmp_db)
    spec = RunSpec(
        kind="orchestrator",
        project_id=tmp_db.project_id,
        question_id="00000000-0000-0000-0000-000000000002",
    )
    await ex.start(spec)
    with pytest.raises(ValueError, match="already has an in-flight task"):
        await ex.start(spec)
    await ex.wait_until_settled(tmp_db.run_id, timeout=5.0)


async def test_start_raises_failure_for_unknown_handler(tmp_db):
    from rumil.run_executor import RunSpec

    ex = RunExecutor(tmp_db)
    spec = RunSpec(
        kind="ingest",
        project_id=tmp_db.project_id,
        question_id="00000000-0000-0000-0000-000000000003",
    )
    with pytest.raises(ValueError, match="No handler registered"):
        await ex.start(spec)


async def test_cancel_running_task_transitions_to_cancelled(tmp_db, fake_handler):
    from rumil.run_executor import RunSpec

    fake_handler["sleep"] = 10.0
    ex = RunExecutor(tmp_db)
    spec = RunSpec(
        kind="orchestrator",
        project_id=tmp_db.project_id,
        question_id="00000000-0000-0000-0000-000000000004",
    )
    run_id = await ex.start(spec)
    # give tracked_scope time to mark_started before we cancel
    await asyncio.sleep(0.05)
    await ex.cancel(run_id, reason="user cancelled")
    view = await ex.wait_until_settled(run_id, timeout=5.0)

    assert view is not None
    assert view.status == RunStatus.CANCELLED
    assert view.cancel_reason == "user cancelled"


async def test_cancel_unknown_run_marks_cancelled_directly(run_db):
    ex = RunExecutor(run_db)
    await ex.cancel(run_db.run_id, reason="external cleanup")
    view = await ex.status(run_db.run_id)
    assert view is not None
    assert view.status == RunStatus.CANCELLED
    assert view.cancel_reason == "external cleanup"


async def test_start_handler_failure_transitions_to_failed(tmp_db, fake_handler):
    from rumil.run_executor import RunSpec

    fake_handler["raise_exc"] = RuntimeError("handler kaboom")
    ex = RunExecutor(tmp_db)
    spec = RunSpec(
        kind="orchestrator",
        project_id=tmp_db.project_id,
        question_id="00000000-0000-0000-0000-000000000005",
    )
    run_id = await ex.start(spec)
    view = await ex.wait_until_settled(run_id, timeout=5.0)

    assert view is not None
    assert view.status == RunStatus.FAILED
    assert view.cancel_reason is not None
    assert "handler kaboom" in view.cancel_reason


async def test_wait_until_settled_returns_current_view_on_timeout(tmp_db, fake_handler):
    from rumil.run_executor import RunSpec

    fake_handler["sleep"] = 10.0
    ex = RunExecutor(tmp_db)
    spec = RunSpec(
        kind="orchestrator",
        project_id=tmp_db.project_id,
        question_id="00000000-0000-0000-0000-000000000006",
    )
    run_id = await ex.start(spec)
    await asyncio.sleep(0.05)
    view = await ex.wait_until_settled(run_id, timeout=0.1)
    assert view is not None
    assert view.status == RunStatus.RUNNING
    # clean up the lingering task
    await ex.cancel(run_id, reason="timeout-test cleanup")
    await ex.wait_until_settled(run_id, timeout=5.0)


async def test_sum_call_costs_returns_zero_when_no_rows(run_db):
    ex = RunExecutor(run_db)
    assert await ex.sum_call_costs(run_db.run_id) == 0


async def test_sum_call_costs_sums_rows(run_db):
    for usd in ("0.10", "1.25", "0.05"):
        await run_db._execute(
            run_db.client.table("call_costs").insert(
                {
                    "run_id": run_db.run_id,
                    "call_id": "00000000-0000-0000-0000-000000000000",
                    "call_type": "assess",
                    "usd": usd,
                }
            )
        )
    ex = RunExecutor(run_db)
    assert await ex.sum_call_costs(run_db.run_id) == 140  # $1.40 → 140 cents


async def test_would_exceed_budget_false_without_cap(run_db):
    ex = RunExecutor(run_db)
    assert await ex.would_exceed_budget(run_db.run_id) is False


async def test_pause_running_run_sets_paused_status(run_db):
    ex = RunExecutor(run_db)
    await ex.mark_started(run_db.run_id)
    await ex.pause(run_db.run_id)
    view = await ex.status(run_db.run_id)
    assert view is not None
    assert view.status == RunStatus.PAUSED
    assert view.paused_at is not None


async def test_pause_noop_on_pending_or_complete(run_db):
    ex = RunExecutor(run_db)
    # pending — no transition
    await ex.pause(run_db.run_id)
    view = await ex.status(run_db.run_id)
    assert view is not None
    assert view.status == RunStatus.PENDING

    # complete — no transition
    await ex.mark_started(run_db.run_id)
    await ex.mark_complete(run_db.run_id)
    await ex.pause(run_db.run_id)
    view = await ex.status(run_db.run_id)
    assert view is not None
    assert view.status == RunStatus.COMPLETE


async def test_resume_clears_paused_at(run_db):
    ex = RunExecutor(run_db)
    await ex.mark_started(run_db.run_id)
    await ex.pause(run_db.run_id)
    await ex.resume(run_db.run_id)
    view = await ex.status(run_db.run_id)
    assert view is not None
    assert view.status == RunStatus.RUNNING
    assert view.paused_at is None


async def test_is_paused_reflects_status(run_db):
    ex = RunExecutor(run_db)
    assert await ex.is_paused(run_db.run_id) is False
    await ex.mark_started(run_db.run_id)
    await ex.pause(run_db.run_id)
    assert await ex.is_paused(run_db.run_id) is True
    await ex.resume(run_db.run_id)
    assert await ex.is_paused(run_db.run_id) is False


async def test_is_paused_false_for_unknown_run(tmp_db):
    ex = RunExecutor(tmp_db)
    assert await ex.is_paused("does-not-exist") is False


async def test_wait_while_paused_returns_when_unpaused(run_db):
    ex = RunExecutor(run_db)
    await ex.mark_started(run_db.run_id)
    await ex.pause(run_db.run_id)

    async def _unpause_soon():
        await asyncio.sleep(0.05)
        await ex.resume(run_db.run_id)

    asyncio.create_task(_unpause_soon())
    await ex.wait_while_paused(run_db.run_id, poll_interval=0.02, max_wait=1.0)
    assert await ex.is_paused(run_db.run_id) is False


async def test_wait_while_paused_respects_max_wait(run_db):
    ex = RunExecutor(run_db)
    await ex.mark_started(run_db.run_id)
    await ex.pause(run_db.run_id)
    # returns after max_wait even though still paused
    await ex.wait_while_paused(run_db.run_id, poll_interval=0.02, max_wait=0.05)
    assert await ex.is_paused(run_db.run_id) is True


async def test_checkpoint_writes_sequential_rows(run_db):
    ex = RunExecutor(run_db)
    seq0 = await ex.checkpoint(run_db.run_id, "orchestrator_tick", {"iter": 0})
    seq1 = await ex.checkpoint(run_db.run_id, "orchestrator_tick", {"iter": 1})
    seq2 = await ex.checkpoint(run_db.run_id, "cost_committed", {"usd_cents": 250})

    assert seq0 == 0
    assert seq1 == 1
    assert seq2 == 2

    all_checkpoints = await ex.list_checkpoints(run_db.run_id)
    assert [c["kind"] for c in all_checkpoints] == [
        "orchestrator_tick",
        "orchestrator_tick",
        "cost_committed",
    ]


async def test_latest_checkpoint_filters_by_kind(run_db):
    ex = RunExecutor(run_db)
    await ex.checkpoint(run_db.run_id, "orchestrator_tick", {"iter": 0})
    await ex.checkpoint(run_db.run_id, "cost_committed", {"usd_cents": 100})
    await ex.checkpoint(run_db.run_id, "orchestrator_tick", {"iter": 1})

    latest_any = await ex.latest_checkpoint(run_db.run_id)
    assert latest_any is not None
    assert latest_any["kind"] == "orchestrator_tick"
    assert latest_any["payload"] == {"iter": 1}

    latest_cost = await ex.latest_checkpoint(run_db.run_id, kind="cost_committed")
    assert latest_cost is not None
    assert latest_cost["payload"] == {"usd_cents": 100}


async def test_latest_checkpoint_returns_none_for_empty_run(run_db):
    ex = RunExecutor(run_db)
    assert await ex.latest_checkpoint(run_db.run_id) is None


async def test_is_resumable_false_for_active_run(tmp_db, fake_handler):
    from rumil.run_executor import RunSpec

    fake_handler["sleep"] = 10.0
    ex = RunExecutor(tmp_db)
    spec = RunSpec(
        kind="orchestrator",
        project_id=tmp_db.project_id,
        question_id="00000000-0000-0000-0000-000000000008",
    )
    run_id = await ex.start(spec)
    await asyncio.sleep(0.05)
    # Active in _ACTIVE_RUNS → not resumable even with checkpoints
    await ex.checkpoint(run_id, "orchestrator_tick", {"iter": 0})
    assert await ex.is_resumable(run_id) is False
    await ex.cancel(run_id, reason="is-resumable-test cleanup")
    await ex.wait_until_settled(run_id, timeout=5.0)


async def test_is_resumable_true_for_crashed_run_with_checkpoints(run_db):
    ex = RunExecutor(run_db)
    # simulate a worker that marked_started and wrote a checkpoint, then died
    await ex.mark_started(run_db.run_id)
    await ex.checkpoint(run_db.run_id, "orchestrator_tick", {"iter": 0})
    assert await ex.is_resumable(run_db.run_id) is True


async def test_is_resumable_false_without_checkpoints(run_db):
    ex = RunExecutor(run_db)
    await ex.mark_started(run_db.run_id)
    # running but no checkpoints → nothing to resume from
    assert await ex.is_resumable(run_db.run_id) is False


async def test_would_exceed_budget_trips_when_spend_reaches_cap(tmp_db, fake_handler):
    from rumil.run_executor import RunSpec

    fake_handler["sleep"] = 10.0
    ex = RunExecutor(tmp_db)
    spec = RunSpec(
        kind="orchestrator",
        project_id=tmp_db.project_id,
        question_id="00000000-0000-0000-0000-000000000007",
        budget_usd=Decimal("1.00"),
    )
    run_id = await ex.start(spec)
    await asyncio.sleep(0.05)
    assert await ex.would_exceed_budget(run_id) is False

    await tmp_db._execute(
        tmp_db.client.table("call_costs").insert(
            {
                "run_id": run_id,
                "call_id": "00000000-0000-0000-0000-000000000000",
                "call_type": "assess",
                "usd": "1.50",
            }
        )
    )
    assert await ex.would_exceed_budget(run_id) is True

    await ex.cancel(run_id, reason="budget-test cleanup")
    await ex.wait_until_settled(run_id, timeout=5.0)


async def test_events_yields_status_transitions(tmp_db, fake_handler):
    from rumil.run_executor import RunSpec

    fake_handler["sleep"] = 10.0
    ex = RunExecutor(tmp_db)
    spec = RunSpec(
        kind="orchestrator",
        project_id=tmp_db.project_id,
        question_id="00000000-0000-0000-0000-000000000100",
    )
    run_id = await ex.start(spec)

    collected: list[RunEvent] = []

    async def _consume():
        async for event in ex.events(run_id):
            collected.append(event)
            # Stop once we see the transition of interest; the handler is
            # still sleeping so the underlying task won't enqueue the
            # sentinel, and we don't want to block forever.
            if event.event == "status_changed" and event.payload.get("new") == "complete":
                return

    consumer = asyncio.create_task(_consume())
    # Give the handler time to enter tracked_scope and emit pending→running,
    # and give _consume() time to subscribe before we force the next transition.
    await asyncio.sleep(0.1)
    await ex.mark_complete(run_id)
    await asyncio.wait_for(consumer, timeout=5.0)
    # Clean up the still-sleeping handler task.
    await ex.cancel(run_id, reason="events-test cleanup")
    await ex.wait_until_settled(run_id, timeout=5.0)

    status_changes = [e for e in collected if e.event == "status_changed"]
    transitions = [(e.payload.get("old"), e.payload.get("new")) for e in status_changes]
    assert ("running", "complete") in transitions
    for event in collected:
        assert event.run_id == run_id


async def test_events_passive_for_unknown_run(tmp_db):
    ex = RunExecutor(tmp_db)
    events = [event async for event in ex.events("does-not-exist")]
    assert events == []


async def test_events_passive_snapshot_for_settled_run(run_db):
    ex = RunExecutor(run_db)
    await ex.mark_started(run_db.run_id)
    await ex.mark_complete(run_db.run_id)
    events = [event async for event in ex.events(run_db.run_id)]
    assert len(events) == 1
    assert events[0].event == "status_changed"
    assert events[0].payload == {"old": None, "new": "complete"}


async def test_events_drops_on_slow_subscriber(tmp_db, fake_handler, mocker):
    from rumil.run_executor import RunSpec

    mocker.patch.object(executor_module, "EVENT_QUEUE_MAXSIZE", 1)
    fake_handler["sleep"] = 10.0
    ex = RunExecutor(tmp_db)
    spec = RunSpec(
        kind="orchestrator",
        project_id=tmp_db.project_id,
        question_id="00000000-0000-0000-0000-000000000101",
    )
    run_id = await ex.start(spec)
    await asyncio.sleep(0.1)

    slow_events: list[RunEvent] = []
    fast_events: list[RunEvent] = []

    async def _slow():
        async for event in ex.events(run_id):
            slow_events.append(event)
            await asyncio.sleep(10.0)

    async def _fast():
        async for event in ex.events(run_id):
            fast_events.append(event)

    slow_task = asyncio.create_task(_slow())
    fast_task = asyncio.create_task(_fast())
    await asyncio.sleep(0.05)

    for _ in range(5):
        await ex.checkpoint(run_id, "orchestrator_tick", {"iter": 0})

    await ex.cancel(run_id, reason="drop-test cleanup")
    await ex.wait_until_settled(run_id, timeout=5.0)
    await asyncio.wait_for(fast_task, timeout=5.0)
    slow_task.cancel()
    try:
        await slow_task
    except asyncio.CancelledError:
        pass

    checkpoint_count = sum(1 for e in fast_events if e.event == "checkpointed")
    assert checkpoint_count == 5
    assert len(slow_events) <= 2


async def test_events_terminates_on_task_settled(tmp_db, fake_handler):
    from rumil.run_executor import RunSpec

    fake_handler["sleep"] = 0.2
    ex = RunExecutor(tmp_db)
    spec = RunSpec(
        kind="orchestrator",
        project_id=tmp_db.project_id,
        question_id="00000000-0000-0000-0000-000000000102",
    )
    run_id = await ex.start(spec)

    async def _consume():
        return [event async for event in ex.events(run_id)]

    consumer = asyncio.create_task(_consume())
    events = await asyncio.wait_for(consumer, timeout=5.0)
    assert any(e.event == "status_changed" for e in events)
