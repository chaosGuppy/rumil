"""Tests for the load_run skill module."""

from __future__ import annotations

import pytest
import pytest_asyncio
from rumil_skills import _runctx, load_run

from rumil.models import Call, CallStatus, CallType, Workspace


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch, tmp_path):
    monkeypatch.setattr(_runctx, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(_runctx, "STATE_FILE", tmp_path / "state" / "rumil-session.json")


async def _noop_close():
    return None


@pytest.fixture
def patch_make_db(monkeypatch, tmp_db):
    async def _fake_make_db(*, prod=False, staged=False, workspace=None, run_id=None):
        return tmp_db, "test-workspace"

    monkeypatch.setattr(load_run, "make_db", _fake_make_db)
    monkeypatch.setattr(tmp_db, "close", _noop_close)
    return tmp_db


@pytest_asyncio.fixture
async def run_with_calls(tmp_db, question_page):
    parent = Call(
        call_type=CallType.FIND_CONSIDERATIONS,
        workspace=Workspace.RESEARCH,
        scope_page_id=question_page.id,
        status=CallStatus.COMPLETE,
    )
    await tmp_db.save_call(parent)
    child = Call(
        call_type=CallType.ASSESS,
        workspace=Workspace.RESEARCH,
        scope_page_id=question_page.id,
        status=CallStatus.COMPLETE,
        parent_call_id=parent.id,
    )
    await tmp_db.save_call(child)
    await tmp_db.save_call_trace(
        parent.id,
        [{"event": "context_built", "ts": "2026-04-13T10:00:00"}],
    )
    await tmp_db.save_call_trace(
        child.id,
        [{"event": "error", "ts": "2026-04-13T10:00:10", "message": "ERR_MARK"}],
    )
    return parent, child


async def test_load_run_prints_tree_with_all_calls(
    capsys, monkeypatch, patch_make_db, tmp_db, run_with_calls
):
    parent, child = run_with_calls
    monkeypatch.setattr("sys.argv", ["load_run", tmp_db.run_id])
    await load_run.main()
    out = capsys.readouterr().out

    assert tmp_db.run_id in out
    assert parent.id[:8] in out
    assert child.id[:8] in out
    assert "find_considerations" in out
    assert "assess" in out
    assert "call tree" in out


async def test_load_run_short_id(capsys, monkeypatch, patch_make_db, tmp_db, run_with_calls):
    parent, _ = run_with_calls
    monkeypatch.setattr("sys.argv", ["load_run", tmp_db.run_id[:8]])
    await load_run.main()
    out = capsys.readouterr().out

    assert parent.id[:8] in out


async def test_load_run_no_matching_run(capsys, monkeypatch, patch_make_db):
    monkeypatch.setattr("sys.argv", ["load_run", "nomatch1"])
    with pytest.raises(SystemExit) as excinfo:
        await load_run.main()
    assert excinfo.value.code == 1
    out = capsys.readouterr().out
    assert "nomatch1" in out


async def test_load_run_event_summary_shows_counts(
    capsys, monkeypatch, patch_make_db, tmp_db, run_with_calls
):
    monkeypatch.setattr("sys.argv", ["load_run", tmp_db.run_id])
    await load_run.main()
    out = capsys.readouterr().out

    assert "context_built" in out
    assert "error" in out
    assert "events:" in out


async def test_load_run_full_mode_includes_verbatim_exchanges(
    capsys, monkeypatch, patch_make_db, tmp_db, run_with_calls
):
    parent, _ = run_with_calls
    await tmp_db.save_llm_exchange(
        call_id=parent.id,
        phase="update_workspace",
        system_prompt="VERBATIM_SYS",
        user_message="VERBATIM_USER",
        response_text="VERBATIM_RESP",
        round_num=1,
    )

    monkeypatch.setattr("sys.argv", ["load_run", tmp_db.run_id, "--full"])
    await load_run.main()
    out = capsys.readouterr().out

    assert "VERBATIM_USER" in out
    assert "VERBATIM_RESP" in out
    assert "full per-call traces" in out
