"""Contract tests for ``run_prioritization_call``.

These tests couple deliberately to the function's interface: they verify
status transitions, return shape, the under-allocation retry, and the
extra-dispatch-def wiring. When the prioritizer rearchitect replaces this
interface, these tests are expected to be rewritten to target the
successor.

LLM plumbing is mocked at the ``run_single_call`` import site inside
``rumil.calls.prioritization``; the real retry branch in
``run_prioritization_call`` executes unchanged.
"""

import pytest

from rumil.calls.dispatches import RECURSE_DISPATCH_DEF
from rumil.calls.prioritization import run_prioritization_call
from rumil.llm import AgentResult
from rumil.models import (
    AssessDispatchPayload,
    CallStatus,
    Dispatch,
    FindConsiderationsMode,
    ScoutDispatchPayload,
)


def _agent_result() -> AgentResult:
    return AgentResult(messages=[{"role": "assistant", "content": "ok"}])


def _make_side_effect(dispatches_per_call: list[list[Dispatch]]):
    """Return a coroutine that scripts successive run_single_call invocations.

    Each element of ``dispatches_per_call`` is the list of dispatches to
    append to ``state.dispatches`` on that invocation.
    """
    calls: list[dict] = []

    async def _side_effect(system_prompt, user_message="", tools=None, **kwargs):
        idx = len(calls)
        calls.append(
            {
                "system_prompt": system_prompt,
                "user_message": user_message,
                "tools": tools,
                **kwargs,
            }
        )
        to_add = dispatches_per_call[idx] if idx < len(dispatches_per_call) else []
        state = kwargs["state"]
        for d in to_add:
            state.dispatches.append(d)
        return _agent_result()

    return _side_effect, calls


@pytest.mark.asyncio
async def test_run_prioritization_call_sets_status_running(
    tmp_db, prioritization_call, question_page, mocker
):
    side_effect, _ = _make_side_effect([[]])
    mocker.patch(
        "rumil.calls.prioritization.run_single_call",
        side_effect=side_effect,
    )

    await run_prioritization_call(
        "task",
        "context",
        prioritization_call,
        tmp_db,
        system_prompt="sys",
    )

    refreshed = await tmp_db.get_call(prioritization_call.id)
    assert refreshed is not None
    assert refreshed.status == CallStatus.RUNNING


@pytest.mark.asyncio
async def test_run_prioritization_call_returns_dispatches(
    tmp_db, prioritization_call, question_page, mocker
):
    scripted = [
        Dispatch(
            call_type=prioritization_call.call_type.ASSESS,
            payload=AssessDispatchPayload(question_id=question_page.id, reason="r1"),
        ),
        Dispatch(
            call_type=prioritization_call.call_type.ASSESS,
            payload=AssessDispatchPayload(question_id=question_page.id, reason="r2"),
        ),
    ]
    side_effect, _ = _make_side_effect([scripted])
    mocker.patch(
        "rumil.calls.prioritization.run_single_call",
        side_effect=side_effect,
    )

    result = await run_prioritization_call(
        "task",
        "context",
        prioritization_call,
        tmp_db,
        system_prompt="sys",
    )

    assert len(result.dispatches) == 2
    reasons = [d.payload.reason for d in result.dispatches]
    assert reasons == ["r1", "r2"]


@pytest.mark.asyncio
async def test_under_allocation_triggers_retry(tmp_db, prioritization_call, question_page, mocker):
    """Cost=1 with budget=10 is under 50% → retry fires in phase prioritization_retry."""
    first_round = [
        Dispatch(
            call_type=prioritization_call.call_type.ASSESS,
            payload=AssessDispatchPayload(question_id=question_page.id),
        ),
    ]
    side_effect, calls = _make_side_effect([first_round, []])
    mocker.patch(
        "rumil.calls.prioritization.run_single_call",
        side_effect=side_effect,
    )

    await run_prioritization_call(
        "task",
        "context",
        prioritization_call,
        tmp_db,
        system_prompt="sys",
        dispatch_budget=10,
    )

    assert len(calls) == 2
    assert calls[0]["phase"] == "prioritization"
    assert calls[1]["phase"] == "prioritization_retry"
    assert calls[1].get("messages") is not None


@pytest.mark.asyncio
async def test_no_retry_when_allocation_sufficient(
    tmp_db, prioritization_call, question_page, mocker
):
    """Scout dispatch with max_rounds=5 against budget=10 is exactly 50% → no retry."""
    first_round = [
        Dispatch(
            call_type=prioritization_call.call_type.FIND_CONSIDERATIONS,
            payload=ScoutDispatchPayload(
                question_id=question_page.id,
                mode=FindConsiderationsMode.ALTERNATE,
                max_rounds=5,
            ),
        ),
    ]
    side_effect, calls = _make_side_effect([first_round])
    mocker.patch(
        "rumil.calls.prioritization.run_single_call",
        side_effect=side_effect,
    )

    await run_prioritization_call(
        "task",
        "context",
        prioritization_call,
        tmp_db,
        system_prompt="sys",
        dispatch_budget=10,
    )

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_no_retry_when_dispatch_budget_none(
    tmp_db, prioritization_call, question_page, mocker
):
    """Dispatch budget unset → the retry branch never evaluates."""
    first_round = [
        Dispatch(
            call_type=prioritization_call.call_type.ASSESS,
            payload=AssessDispatchPayload(question_id=question_page.id),
        ),
    ]
    side_effect, calls = _make_side_effect([first_round])
    mocker.patch(
        "rumil.calls.prioritization.run_single_call",
        side_effect=side_effect,
    )

    await run_prioritization_call(
        "task",
        "context",
        prioritization_call,
        tmp_db,
        system_prompt="sys",
        dispatch_budget=None,
    )

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_recurse_defs_included_when_passed_as_extra(
    tmp_db, prioritization_call, question_page, mocker
):
    """extra_dispatch_defs add tools to the list handed to run_single_call."""
    side_effect, calls = _make_side_effect([[]])
    mocker.patch(
        "rumil.calls.prioritization.run_single_call",
        side_effect=side_effect,
    )

    await run_prioritization_call(
        "task",
        "context",
        prioritization_call,
        tmp_db,
        system_prompt="sys",
        extra_dispatch_defs=[RECURSE_DISPATCH_DEF],
    )

    tool_names = {t.name for t in calls[0]["tools"]}
    assert RECURSE_DISPATCH_DEF.name in tool_names
