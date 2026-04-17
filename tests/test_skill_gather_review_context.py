"""Tests for rumil_skills.gather_review_context — non-LLM context assembly."""

from __future__ import annotations

import pytest
from rumil_skills import _runctx, gather_review_context, scan_log

from rumil.models import (
    Call,
    CallStatus,
    CallType,
    LinkType,
    Page,
    PageLayer,
    PageLink,
    PageType,
    Workspace,
)


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch, tmp_path):
    monkeypatch.setattr(_runctx, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(_runctx, "STATE_FILE", tmp_path / "state" / "rumil-session.json")
    monkeypatch.setattr(scan_log, "SCAN_LOG_PATH", tmp_path / "state" / "rumil-scan-log.json")


@pytest.fixture
def patch_make_db(monkeypatch, tmp_db):
    async def _fake_make_db(*, prod=False, staged=False, workspace=None, run_id=None):
        return tmp_db, "test-workspace"

    async def _noop_close():
        return None

    monkeypatch.setattr(gather_review_context, "make_db", _fake_make_db)
    monkeypatch.setattr(tmp_db, "close", _noop_close)
    return tmp_db


async def test_recent_calls_returns_most_recent_first(tmp_db, question_page):
    older = Call(
        call_type=CallType.FIND_CONSIDERATIONS,
        workspace=Workspace.RESEARCH,
        scope_page_id=question_page.id,
        status=CallStatus.COMPLETE,
    )
    await tmp_db.save_call(older)
    newer = Call(
        call_type=CallType.ASSESS,
        workspace=Workspace.RESEARCH,
        scope_page_id=question_page.id,
        status=CallStatus.COMPLETE,
    )
    await tmp_db.save_call(newer)

    rows = await gather_review_context._recent_calls(tmp_db, question_page.id, 10)

    ids = [r["id"] for r in rows]
    assert older.id in ids
    assert newer.id in ids


async def test_recent_calls_limits_count(tmp_db, question_page):
    for _ in range(5):
        call = Call(
            call_type=CallType.FIND_CONSIDERATIONS,
            workspace=Workspace.RESEARCH,
            scope_page_id=question_page.id,
            status=CallStatus.COMPLETE,
        )
        await tmp_db.save_call(call)

    rows = await gather_review_context._recent_calls(tmp_db, question_page.id, 2)
    assert len(rows) == 2


async def test_final_exchange_returns_latest(tmp_db, question_page, scout_call):
    await tmp_db.save_llm_exchange(
        call_id=scout_call.id,
        phase="update_workspace",
        system_prompt="sys",
        user_message="msg1",
        response_text="first response",
        round_num=1,
    )
    await tmp_db.save_llm_exchange(
        call_id=scout_call.id,
        phase="update_workspace",
        system_prompt="sys",
        user_message="msg2",
        response_text="final response",
        round_num=2,
    )

    final = await gather_review_context._final_exchange(tmp_db, scout_call.id)
    assert final is not None
    assert final["response_text"] == "final response"
    assert final["round"] == 2


async def test_final_exchange_none_when_no_exchanges(tmp_db, scout_call):
    final = await gather_review_context._final_exchange(tmp_db, scout_call.id)
    assert final is None


def test_format_call_brief_includes_id_and_type():
    call = {
        "id": "aaaabbbbccccdddd",
        "call_type": "find_considerations",
        "status": "complete",
        "cost_usd": 0.025,
        "created_at": "2025-01-01T00:00:00",
        "trace_json": [],
        "result_summary": "",
        "review_json": {},
    }
    out = gather_review_context._format_call_brief(call, None, None)
    assert "aaaabbbb" in out
    assert "find_considerations" in out
    assert "complete" in out
    assert "$0.025" in out


def test_format_call_brief_renders_scan_verdict():
    call = {
        "id": "aaaabbbbccccdddd",
        "call_type": "assess",
        "status": "complete",
        "cost_usd": None,
        "created_at": "2025-01-01T00:00:00",
        "trace_json": [],
        "result_summary": "",
        "review_json": {},
    }
    scan = {
        "verdict": "confused",
        "severity": 4,
        "primary_symptom": "scope_drift",
        "evidence": ["quote A", "quote B"],
        "suggested_action": "redispatch",
    }
    out = gather_review_context._format_call_brief(call, None, scan)
    assert "confused" in out
    assert "s4" in out
    assert "scope_drift" in out
    assert "redispatch" in out


def test_format_call_brief_includes_final_exchange_response():
    call = {
        "id": "aaaabbbbccccdddd",
        "call_type": "find_considerations",
        "status": "complete",
        "cost_usd": 0.01,
        "created_at": "2025-01-01T00:00:00",
        "trace_json": [],
        "result_summary": "",
        "review_json": {},
    }
    final_ex = {
        "phase": "update_workspace",
        "round": 3,
        "response_text": "here is a final response containing key info",
        "error": None,
    }
    out = gather_review_context._format_call_brief(call, final_ex, None)
    assert "final response" in out
    assert "key info" in out


def test_format_call_brief_renders_trace_error():
    call = {
        "id": "aaaabbbbccccdddd",
        "call_type": "find_considerations",
        "status": "failed",
        "cost_usd": 0.01,
        "created_at": "2025-01-01T00:00:00",
        "trace_json": [
            {"event": "context_built"},
            {"event": "error", "message": "something broke"},
        ],
        "result_summary": "",
        "review_json": {},
    }
    out = gather_review_context._format_call_brief(call, None, None)
    assert "ERROR" in out
    assert "something broke" in out


def test_format_call_brief_renders_review_fields():
    call = {
        "id": "aaaabbbbccccdddd",
        "call_type": "assess",
        "status": "complete",
        "cost_usd": 0.01,
        "created_at": "2025-01-01T00:00:00",
        "trace_json": [],
        "result_summary": "",
        "review_json": {
            "confidence_in_output": 1,
            "what_was_missing": "the prior research",
            "remaining_fruit": "more scouting possible",
        },
    }
    out = gather_review_context._format_call_brief(call, None, None)
    assert "confidence_in_output" in out
    assert "the prior research" in out
    assert "remaining_fruit" in out


async def test_main_unknown_question_exits(capsys, monkeypatch, patch_make_db):
    monkeypatch.setattr("sys.argv", ["gather_review_context", "deadbeef"])
    with pytest.raises(SystemExit) as excinfo:
        await gather_review_context.main()
    assert excinfo.value.code == 1
    out = capsys.readouterr().out
    assert "deadbeef" in out


async def test_main_prints_subtree_and_diagnostics(
    capsys, monkeypatch, patch_make_db, question_page
):
    monkeypatch.setattr("sys.argv", ["gather_review_context", question_page.id])
    await gather_review_context.main()
    out = capsys.readouterr().out

    assert "research subtree" in out
    assert "shape diagnostics" in out
    assert "recent calls" in out
    assert question_page.id[:8] in out


async def test_main_lists_recent_calls_on_question(
    capsys, monkeypatch, patch_make_db, tmp_db, question_page, scout_call
):
    monkeypatch.setattr(
        "sys.argv",
        ["gather_review_context", question_page.id, "--call-limit", "5"],
    )
    await gather_review_context.main()
    out = capsys.readouterr().out

    assert scout_call.id[:8] in out


async def test_main_reports_scanner_verdicts_from_log(
    capsys, monkeypatch, patch_make_db, tmp_db, question_page, scout_call, tmp_path
):
    log = scan_log.load_scan_log()
    scan_log.record_scan(
        log,
        scout_call.id,
        model="claude-haiku",
        verdict="confused",
        severity=4,
        primary_symptom="DISTINCTIVE_SYMPTOM_TOKEN",
        evidence=["q1"],
        suggested_action="redispatch",
    )
    scan_log.save_scan_log(log)

    monkeypatch.setattr(
        "sys.argv",
        ["gather_review_context", question_page.id],
    )
    await gather_review_context.main()
    out = capsys.readouterr().out

    assert "DISTINCTIVE_SYMPTOM_TOKEN" in out


async def test_main_subtree_includes_child_question(
    capsys, monkeypatch, patch_make_db, tmp_db, question_page
):
    child = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="Child body",
        headline="A very DISTINCTIVECHILD child",
    )
    await tmp_db.save_page(child)
    await tmp_db.save_link(
        PageLink(
            from_page_id=question_page.id,
            to_page_id=child.id,
            link_type=LinkType.CHILD_QUESTION,
        )
    )

    monkeypatch.setattr(
        "sys.argv",
        ["gather_review_context", question_page.id],
    )
    await gather_review_context.main()
    out = capsys.readouterr().out
    assert "DISTINCTIVECHILD" in out
