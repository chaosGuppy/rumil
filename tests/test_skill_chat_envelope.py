"""Tests for rumil_skills.chat_envelope and _runctx.ensure_chat_envelope."""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio

from rumil.database import DB
from rumil.models import CallType
from rumil_skills import _runctx


@pytest.fixture
def _isolated_state(tmp_path, monkeypatch):
    """Redirect session-state file into tmp_path."""
    monkeypatch.setattr(_runctx, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(
        _runctx, "STATE_FILE", tmp_path / "state" / "rumil-session.json"
    )
    return tmp_path


@pytest_asyncio.fixture
async def envelope_cleanup():
    """Track envelope run_ids so we can tear down every row they wrote."""
    run_ids: list[str] = []

    yield run_ids

    for run_id in run_ids:
        cleanup_db = await DB.create(run_id=run_id)
        try:
            await cleanup_db.delete_run_data(delete_project=True)
        finally:
            await cleanup_db.close()


def _set_workspace(workspace: str) -> None:
    state = _runctx.load_session_state()
    state.workspace = workspace
    _runctx.save_session_state(state)


async def test_ensure_creates_new_envelope(_isolated_state, envelope_cleanup):
    workspace = f"test-envelope-{uuid.uuid4().hex[:8]}"
    _set_workspace(workspace)

    db, call = await _runctx.ensure_chat_envelope()
    envelope_cleanup.append(db.run_id)
    try:
        assert call.call_type == CallType.CLAUDE_CODE_DIRECT

        fetched = await db.get_call(call.id)
        assert fetched is not None
        assert fetched.call_type == CallType.CLAUDE_CODE_DIRECT

        state = _runctx.load_session_state()
        assert state.chat_envelope is not None
        assert state.chat_envelope["call_id"] == call.id
        assert state.chat_envelope["run_id"] == db.run_id
        assert state.chat_envelope["workspace"] == workspace

        run_rows = (
            await db._execute(db.client.table("runs").select("*").eq("id", db.run_id))
        ).data
        assert len(run_rows) == 1
        assert run_rows[0]["config"]["envelope"] is True
        assert run_rows[0]["config"]["origin"] == "claude-code"
    finally:
        await db.close()


async def test_ensure_reuses_existing_envelope(_isolated_state, envelope_cleanup):
    workspace = f"test-envelope-{uuid.uuid4().hex[:8]}"
    _set_workspace(workspace)

    db1, call1 = await _runctx.ensure_chat_envelope()
    envelope_cleanup.append(db1.run_id)
    await db1.close()

    db2, call2 = await _runctx.ensure_chat_envelope()
    try:
        assert call2.id == call1.id
        assert db2.run_id == db1.run_id
    finally:
        await db2.close()


async def test_ensure_drops_envelope_when_workspace_changes(
    _isolated_state, envelope_cleanup
):
    workspace_a = f"test-env-a-{uuid.uuid4().hex[:8]}"
    workspace_b = f"test-env-b-{uuid.uuid4().hex[:8]}"

    _set_workspace(workspace_a)
    db1, call1 = await _runctx.ensure_chat_envelope()
    envelope_cleanup.append(db1.run_id)
    await db1.close()

    _set_workspace(workspace_b)
    db2, call2 = await _runctx.ensure_chat_envelope()
    envelope_cleanup.append(db2.run_id)
    try:
        assert call2.id != call1.id
        assert db2.run_id != db1.run_id
        state = _runctx.load_session_state()
        assert state.chat_envelope is not None
        assert state.chat_envelope["call_id"] == call2.id
        assert state.chat_envelope["workspace"] == workspace_b
    finally:
        await db2.close()


async def test_ensure_recreates_when_call_row_missing(
    _isolated_state, envelope_cleanup
):
    """A stale pointer whose call has been deleted triggers a fresh envelope."""
    workspace = f"test-envelope-{uuid.uuid4().hex[:8]}"
    _set_workspace(workspace)

    db1, call1 = await _runctx.ensure_chat_envelope()
    envelope_cleanup.append(db1.run_id)
    await db1._execute(db1.client.table("calls").delete().eq("id", call1.id))
    await db1.close()

    db2, call2 = await _runctx.ensure_chat_envelope()
    envelope_cleanup.append(db2.run_id)
    try:
        assert call2.id != call1.id
    finally:
        await db2.close()


async def test_clear_chat_envelope_removes_pointer(_isolated_state, envelope_cleanup):
    workspace = f"test-envelope-{uuid.uuid4().hex[:8]}"
    _set_workspace(workspace)

    db, _call = await _runctx.ensure_chat_envelope()
    envelope_cleanup.append(db.run_id)
    await db.close()

    assert _runctx.load_session_state().chat_envelope is not None

    _runctx.clear_chat_envelope()

    assert _runctx.load_session_state().chat_envelope is None


async def test_clear_then_ensure_creates_fresh_envelope(
    _isolated_state, envelope_cleanup
):
    workspace = f"test-envelope-{uuid.uuid4().hex[:8]}"
    _set_workspace(workspace)

    db1, call1 = await _runctx.ensure_chat_envelope()
    envelope_cleanup.append(db1.run_id)
    await db1.close()

    _runctx.clear_chat_envelope()

    db2, call2 = await _runctx.ensure_chat_envelope()
    envelope_cleanup.append(db2.run_id)
    try:
        assert call2.id != call1.id
    finally:
        await db2.close()
