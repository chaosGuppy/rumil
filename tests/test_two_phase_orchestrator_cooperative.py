"""Cooperative RunExecutor wiring in ``TwoPhaseOrchestrator``.

The orchestrator polls three control-plane surfaces at the top of each
dispatch batch:

- ``wait_while_paused`` — blocks while the run is paused
- ``would_exceed_budget`` — stops dispatching when the dollar cap trips
- ``checkpoint("orchestrator_tick", ...)`` — emits state after every batch

These tests pin the contract through the public ``run()`` entry point,
reusing the shared ``prio_harness`` fixture so LLM plumbing is mocked
but orchestrator control-flow runs for real.
"""

from __future__ import annotations

import asyncio

import pytest

from rumil.calls.common import RunCallResult
from rumil.models import (
    CallType,
    Dispatch,
    FindConsiderationsMode,
    ScoutDispatchPayload,
)
from rumil.orchestrators.two_phase import TwoPhaseOrchestrator
from rumil.run_executor import RunExecutor


def _scout_dispatch(question_id: str, reason: str = "") -> Dispatch:
    return Dispatch(
        call_type=CallType.FIND_CONSIDERATIONS,
        payload=ScoutDispatchPayload(
            question_id=question_id,
            mode=FindConsiderationsMode.ALTERNATE,
            max_rounds=1,
            reason=reason,
        ),
    )


async def _seed_run_row(db) -> None:
    await db.create_run(name="cooperative-test", question_id=None, config={})


@pytest.mark.asyncio
async def test_waits_while_paused(tmp_db, question_page, prio_harness, mocker):
    """While ``is_paused`` returns True, no new dispatches land.

    We pause the run before ``run()`` is called, let the orchestrator
    spin in the pause-poll loop for a while, then resume. After resume,
    the scripted queue drains and dispatches are observed. The key
    invariant: during the paused window no calls were dispatched.
    """
    await _seed_run_row(tmp_db)
    await tmp_db.init_budget(10)
    prio_harness.prio_queue = [
        RunCallResult(dispatches=[_scout_dispatch(question_page.id)]),
        RunCallResult(dispatches=[_scout_dispatch(question_page.id)]),
    ]

    executor = RunExecutor(tmp_db)
    await executor.mark_started(tmp_db.run_id)
    await executor.pause(tmp_db.run_id)

    orch = TwoPhaseOrchestrator(tmp_db)
    mocker.patch.object(
        orch._executor,
        "wait_while_paused",
        side_effect=executor.wait_while_paused,
    )

    async def _run_and_resume():
        async def _resume_soon():
            await asyncio.sleep(0.15)
            assert len(prio_harness.dispatched) == 0, (
                "Dispatches landed before resume — pause is not enforced"
            )
            await executor.resume(tmp_db.run_id)

        resume_task = asyncio.create_task(_resume_soon())
        mocker.patch.object(
            orch._executor,
            "wait_while_paused",
            side_effect=lambda run_id, poll_interval=1.0: executor.wait_while_paused(
                run_id, poll_interval=0.02
            ),
        )
        await orch.run(question_page.id)
        await resume_task

    await _run_and_resume()

    assert len(prio_harness.dispatched) > 0, "After resume, scripted dispatches should have landed"


@pytest.mark.asyncio
async def test_stops_when_budget_exceeded(tmp_db, question_page, prio_harness, mocker):
    """When ``would_exceed_budget`` reports True, the loop exits cleanly.

    We arrange a scripted queue that would otherwise drive many
    dispatches, but patch ``would_exceed_budget`` on the orchestrator's
    executor to return True. The orchestrator must exit before landing
    any new dispatch and must not raise.
    """
    await _seed_run_row(tmp_db)
    await tmp_db.init_budget(20)
    prio_harness.prio_queue = [
        RunCallResult(dispatches=[_scout_dispatch(question_page.id)]) for _ in range(5)
    ]

    orch = TwoPhaseOrchestrator(tmp_db)

    async def _always_over(_run_id):
        return True

    mocker.patch.object(orch._executor, "would_exceed_budget", side_effect=_always_over)

    await orch.run(question_page.id)

    assert len(prio_harness.dispatched) == 0, (
        "Budget cap should gate ALL new dispatches, including the first one"
    )
    assert len(prio_harness.prio_queue) == 5, (
        "Prio queue should not be touched when the budget cap trips at the top"
    )


@pytest.mark.asyncio
async def test_stops_when_budget_exceeded_after_first_batch(
    tmp_db, question_page, prio_harness, mocker
):
    """Budget cap evaluated per-batch: first batch lands, cap trips, loop exits.

    Ensures any in-flight dispatch completes gracefully — the first
    scripted batch dispatches normally, then the next iteration sees
    the cap tripped and exits. No exception should escape.
    """
    await _seed_run_row(tmp_db)
    await tmp_db.init_budget(20)
    prio_harness.prio_queue = [
        RunCallResult(dispatches=[_scout_dispatch(question_page.id)]),
        RunCallResult(dispatches=[_scout_dispatch(question_page.id)]),
        RunCallResult(dispatches=[_scout_dispatch(question_page.id)]),
    ]

    orch = TwoPhaseOrchestrator(tmp_db)

    calls = {"n": 0}

    async def _trip_after_first(_run_id):
        calls["n"] += 1
        return calls["n"] > 1

    mocker.patch.object(orch._executor, "would_exceed_budget", side_effect=_trip_after_first)

    await orch.run(question_page.id)

    assert len(prio_harness.dispatched) >= 1, (
        "First batch should complete before the cap-check runs on the next iteration"
    )
    assert len(prio_harness.prio_queue) >= 1, (
        "Once cap trips, remaining prio queue should not be consumed"
    )


@pytest.mark.asyncio
async def test_writes_orchestrator_tick_checkpoints(tmp_db, question_page, prio_harness):
    """Each completed dispatch batch emits one ``orchestrator_tick`` checkpoint.

    The payload carries ``iteration``, ``pending_dispatches``, and
    ``budget_remaining`` so a future resume can re-hydrate.
    """
    await _seed_run_row(tmp_db)
    await tmp_db.init_budget(10)
    prio_harness.prio_queue = [
        RunCallResult(dispatches=[_scout_dispatch(question_page.id)]),
        RunCallResult(dispatches=[_scout_dispatch(question_page.id)]),
        RunCallResult(dispatches=[]),
    ]

    orch = TwoPhaseOrchestrator(tmp_db)
    await orch.run(question_page.id)

    ticks = await orch._executor.list_checkpoints(tmp_db.run_id, kind="orchestrator_tick")
    assert len(ticks) >= 1, "At least one orchestrator_tick should be written"
    for t in ticks:
        payload = t["payload"]
        assert "iteration" in payload
        assert "pending_dispatches" in payload
        assert "budget_remaining" in payload

    iterations = [t["payload"]["iteration"] for t in ticks]
    assert iterations == sorted(iterations), "Tick iterations should be monotonic across a run"


@pytest.mark.asyncio
@pytest.mark.skip(
    reason=(
        "TwoPhase's initial-prio emptiness path runs the default-scout "
        "synthesizer (see two_phase.py:510), which produces dispatches "
        "and writes a tick. The 'no tick when no dispatches' contract "
        "holds at the plan layer but is invisible to this test because "
        "the synthesizer intercepts. Rework this once the synthesizer "
        "path can be disabled from a fixture."
    )
)
async def test_does_not_write_tick_when_no_dispatches(tmp_db, question_page, prio_harness):
    """When the first prio plans nothing, no tick is written.

    The checkpoint lives AFTER the batch-commit block, so empty-plan
    early-exits should not create an orchestrator_tick row. This guards
    the "last known-good state" contract — ticks correspond to batches
    that actually landed.
    """
    await _seed_run_row(tmp_db)
    await tmp_db.init_budget(10)
    prio_harness.prio_queue = [RunCallResult(dispatches=[])]

    orch = TwoPhaseOrchestrator(tmp_db)
    await orch.run(question_page.id)

    ticks = await orch._executor.list_checkpoints(tmp_db.run_id, kind="orchestrator_tick")
    assert ticks == []
