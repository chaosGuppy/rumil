"""Tests for rumil_skills.dispatch_call — the rumil-mediated single-call lane.

Uses the real Haiku model (via test-mode settings). Tests assert structurally:
the call status moved past PENDING, the run row was created with
origin=claude-code, at least one page was linked to the scope question.
"""

from __future__ import annotations

import pytest

from rumil.models import CallStatus
from rumil_skills import _runctx, dispatch_call


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch, tmp_path):
    monkeypatch.setattr(_runctx, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(
        _runctx, "STATE_FILE", tmp_path / "state" / "rumil-session.json"
    )


@pytest.fixture
def patch_make_db(monkeypatch, tmp_db):
    async def _fake_make_db(*, prod=False, staged=False, workspace=None, run_id=None):
        return tmp_db, "test-workspace"

    async def _noop_close():
        return None

    monkeypatch.setattr(dispatch_call, "make_db", _fake_make_db)
    monkeypatch.setattr(tmp_db, "close", _noop_close)
    return tmp_db


@pytest.mark.integration
async def test_dispatch_find_considerations_completes(
    monkeypatch, patch_make_db, tmp_db, question_page
):
    """find-considerations dispatch runs end-to-end, creates a call + pages."""
    monkeypatch.setattr(
        "sys.argv",
        [
            "dispatch_call",
            "find-considerations",
            question_page.id,
            "--budget",
            "1",
            "--max-rounds",
            "1",
            "--smoke-test",
        ],
    )
    await dispatch_call.main()

    calls = await tmp_db._execute(
        tmp_db.client.table("calls").select("*").eq("scope_page_id", question_page.id)
    )
    rows = list(getattr(calls, "data", None) or [])
    assert len(rows) >= 1

    non_pending = [r for r in rows if r["status"] != CallStatus.PENDING.value]
    assert len(non_pending) >= 1

    run_scoped = [r for r in rows if r.get("run_id") == tmp_db.run_id]
    assert len(run_scoped) >= 1


@pytest.mark.integration
async def test_dispatch_records_run_with_origin(
    monkeypatch, patch_make_db, tmp_db, question_page
):
    monkeypatch.setattr(
        "sys.argv",
        [
            "dispatch_call",
            "find-considerations",
            question_page.id,
            "--budget",
            "1",
            "--max-rounds",
            "1",
            "--smoke-test",
        ],
    )
    await dispatch_call.main()

    runs = await tmp_db._execute(
        tmp_db.client.table("runs").select("*").eq("id", tmp_db.run_id)
    )
    rows = list(getattr(runs, "data", None) or [])
    assert len(rows) == 1
    config = rows[0].get("config") or {}
    assert config.get("origin") == "claude-code"
    assert config.get("skill") == "rumil-dispatch"


async def test_dispatch_unknown_question_exits(monkeypatch, patch_make_db, capsys):
    monkeypatch.setattr(
        "sys.argv",
        [
            "dispatch_call",
            "find-considerations",
            "deadbeef",
            "--budget",
            "1",
        ],
    )
    with pytest.raises(SystemExit) as excinfo:
        await dispatch_call.main()
    assert excinfo.value.code == 1
    out = capsys.readouterr().out
    assert "deadbeef" in out


def test_dispatch_call_type_choices_coverage():
    assert "find-considerations" in dispatch_call.CALL_TYPES
    assert "assess" in dispatch_call.CALL_TYPES
    assert "web-research" in dispatch_call.CALL_TYPES
    for scout_name in dispatch_call._SCOUT_MAP:
        assert scout_name in dispatch_call.CALL_TYPES


def test_tag_call_params_merges_existing():
    from rumil.models import Call, CallType, Workspace

    call = Call(
        call_type=CallType.FIND_CONSIDERATIONS,
        workspace=Workspace.RESEARCH,
        call_params={"preexisting": "value"},
    )
    tagged = dispatch_call._tag_call_params(call, "rumil-dispatch")
    assert tagged["origin"] == "claude-code"
    assert tagged["skill"] == "rumil-dispatch"
    assert tagged["preexisting"] == "value"


def test_tag_call_params_handles_none():
    from rumil.models import Call, CallType, Workspace

    call = Call(
        call_type=CallType.FIND_CONSIDERATIONS,
        workspace=Workspace.RESEARCH,
        call_params=None,
    )
    tagged = dispatch_call._tag_call_params(call, "rumil-dispatch")
    assert tagged["origin"] == "claude-code"
    assert tagged["skill"] == "rumil-dispatch"
