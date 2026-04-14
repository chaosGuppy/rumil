"""Tests for the CallTrace error-handling contract.

Covers the two post/in-progress paths:
- ``record()`` is the default mid-call diagnostic path — it logs and swallows
  DB write failures so a flaky trace doesn't kill an in-progress call.
- ``record_strict()`` raises ``TraceRecordError`` on DB write failure — used
  right after workspace mutations to fail loud so the frontend view never
  silently falls out of sync with live DB state.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from rumil.models import Call, CallType, Workspace
from rumil.tracing.trace_events import WarningEvent
from rumil.tracing.tracer import CallTrace, TraceRecordError


@pytest_asyncio.fixture
async def trace_call(tmp_db):
    call = Call(
        call_type=CallType.CLAUDE_CODE_DIRECT,
        workspace=Workspace.RESEARCH,
    )
    await tmp_db.save_call(call)
    return call


async def test_record_persists_event_on_success(tmp_db, trace_call):
    trace = CallTrace(trace_call.id, tmp_db)
    await trace.record(WarningEvent(message="hello from mid-call"))

    events = await tmp_db.get_call_trace(trace_call.id)
    assert any(
        ev.get("event") == "warning" and ev.get("message") == "hello from mid-call"
        for ev in events
    )


async def test_record_swallows_db_failure(tmp_db, trace_call, mocker, caplog):
    trace = CallTrace(trace_call.id, tmp_db)
    mocker.patch.object(
        tmp_db,
        "save_call_trace",
        side_effect=RuntimeError("simulated trace write failure"),
    )
    with caplog.at_level("ERROR"):
        await trace.record(WarningEvent(message="in-progress diagnostic"))
    assert any(
        "Failed to persist trace event" in rec.getMessage() for rec in caplog.records
    )


async def test_record_strict_persists_event_on_success(tmp_db, trace_call):
    trace = CallTrace(trace_call.id, tmp_db)
    await trace.record_strict(WarningEvent(message="post-mutation record"))

    events = await tmp_db.get_call_trace(trace_call.id)
    assert any(
        ev.get("event") == "warning" and ev.get("message") == "post-mutation record"
        for ev in events
    )


async def test_record_strict_raises_on_db_failure(tmp_db, trace_call, mocker):
    trace = CallTrace(trace_call.id, tmp_db)
    mocker.patch.object(
        tmp_db,
        "save_call_trace",
        side_effect=RuntimeError("simulated trace write failure"),
    )
    with pytest.raises(TraceRecordError, match="simulated trace write failure"):
        await trace.record_strict(WarningEvent(message="post-mutation, must land"))


async def test_record_strict_chains_original_exception(tmp_db, trace_call, mocker):
    trace = CallTrace(trace_call.id, tmp_db)
    boom = RuntimeError("original boom")
    mocker.patch.object(tmp_db, "save_call_trace", side_effect=boom)

    with pytest.raises(TraceRecordError) as excinfo:
        await trace.record_strict(WarningEvent(message="x"))
    assert excinfo.value.__cause__ is boom


async def test_record_strict_disabled_returns_without_raising(
    tmp_db, trace_call, mocker, monkeypatch
):
    from rumil import settings as settings_mod

    mocker.patch.object(
        tmp_db,
        "save_call_trace",
        side_effect=RuntimeError("would explode if called"),
    )
    with settings_mod.override_settings(tracing_enabled=False):
        trace = CallTrace(trace_call.id, tmp_db)
        await trace.record_strict(WarningEvent(message="x"))


async def test_record_disabled_returns_without_writing(tmp_db, trace_call, mocker):
    from rumil import settings as settings_mod

    save_spy = mocker.patch.object(tmp_db, "save_call_trace")
    with settings_mod.override_settings(tracing_enabled=False):
        trace = CallTrace(trace_call.id, tmp_db)
        await trace.record(WarningEvent(message="x"))
    save_spy.assert_not_called()


async def test_record_round_moves_raises_when_trace_fails(tmp_db, trace_call, mocker):
    """Post-mutation: ``record_round_moves`` uses ``record_strict`` so that when
    tools have already executed and written to the DB, a failing trace write
    fails loud rather than leaving the trace out of sync with DB state.
    """
    from rumil.calls.common import record_round_moves
    from rumil.models import Move, MoveType
    from rumil.moves.base import MoveState
    from rumil.moves.create_claim import CreateClaimPayload
    from rumil.tracing.tracer import set_trace

    state = MoveState(call=trace_call, db=tmp_db)
    payload = CreateClaimPayload(
        headline="post-mutation trace failure",
        content="The move landed before the trace was attempted.",
        credence=5,
        robustness=2,
        workspace=Workspace.RESEARCH,
        supersedes=None,
        change_magnitude=None,
    )
    state.moves.append(Move(move_type=MoveType.CREATE_CLAIM, payload=payload))
    state.move_created_ids.append([])
    state.move_trace_extras.append({})

    trace = CallTrace(trace_call.id, tmp_db)
    set_trace(trace)

    mocker.patch.object(
        tmp_db,
        "save_call_trace",
        side_effect=RuntimeError("simulated trace write failure"),
    )
    mocker.patch(
        "rumil.calls.common.moves_to_trace_event",
        return_value=WarningEvent(message="stub"),
    )

    with pytest.raises(TraceRecordError, match="simulated trace write failure"):
        await record_round_moves(state=state, db=tmp_db)
