"""Tests for GlobalPrioOrchestrator loop termination behavior."""

import asyncio

import pytest
import pytest_asyncio

from rumil.orchestrators.global_prio import GlobalPrioOrchestrator


async def _noop_trigger() -> None:
    return


async def _no_creation_turn(root_id: str) -> dict:
    return {"created_questions": [], "dispatches": []}


@pytest_asyncio.fixture
async def orch(tmp_db):
    """Create a GlobalPrioOrchestrator with a global budget cap."""
    o = GlobalPrioOrchestrator(tmp_db)
    o._global_cap = 50
    o._global_consumed = 0
    return o


@pytest.mark.asyncio
async def test_global_loop_exits_when_local_done_and_no_creation(
    orch,
    mocker,
):
    """When the local task is done and a global turn creates nothing,
    the loop should exit instead of spinning forever."""
    trigger_mock = mocker.patch.object(
        orch, "_wait_for_trigger", side_effect=_noop_trigger,
    )
    turn_mock = mocker.patch.object(
        orch, "_global_turn", side_effect=_no_creation_turn,
    )

    done_future: asyncio.Future[None] = asyncio.get_event_loop().create_future()
    done_future.set_result(None)
    orch._local_task = done_future

    await asyncio.wait_for(orch._global_loop("fake-root-id"), timeout=5)

    assert turn_mock.call_count == 1


@pytest.mark.asyncio
async def test_global_loop_exits_when_no_local_task_and_no_creation(
    orch,
    mocker,
):
    """When there is no local task at all (global-only mode) and a global
    turn creates nothing, the loop should exit."""
    mocker.patch.object(
        orch, "_wait_for_trigger", side_effect=_noop_trigger,
    )
    turn_mock = mocker.patch.object(
        orch, "_global_turn", side_effect=_no_creation_turn,
    )

    orch._local_task = None

    await asyncio.wait_for(orch._global_loop("fake-root-id"), timeout=5)

    assert turn_mock.call_count == 1
