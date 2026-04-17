"""Tests for rumil_skills.find_confusion — heuristic-mode scan of recent calls.

Only heuristic paths (no --deep LLM calls). The heuristic logic is pure
DB inspection plus scoring; no LLM involvement.
"""

from __future__ import annotations

import pytest
from rumil_skills import _runctx, find_confusion

from rumil.models import (
    Call,
    CallStatus,
    CallType,
    Workspace,
)


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch, tmp_path):
    monkeypatch.setattr(_runctx, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(_runctx, "STATE_FILE", tmp_path / "state" / "rumil-session.json")


async def _save_raw_trace(db, call_id: str, events: list[dict]) -> None:
    await db.save_call_trace(call_id, events)


async def _make_call(
    db,
    question_id: str,
    *,
    call_type: CallType = CallType.FIND_CONSIDERATIONS,
    status: CallStatus = CallStatus.COMPLETE,
    cost_usd: float | None = 0.01,
) -> Call:
    call = Call(
        call_type=call_type,
        workspace=Workspace.RESEARCH,
        scope_page_id=question_id,
        status=status,
        cost_usd=cost_usd,
    )
    await db.save_call(call)
    return call


async def test_fetch_recent_calls_scoped_to_project(tmp_db, question_page):
    call = await _make_call(tmp_db, question_page.id)

    rows = await find_confusion._fetch_recent_calls(tmp_db, limit=10)
    ids = [r["id"] for r in rows]
    assert call.id in ids


async def test_score_heuristics_flags_failed_call(tmp_db, question_page):
    good = await _make_call(tmp_db, question_page.id, status=CallStatus.COMPLETE)
    bad = await _make_call(tmp_db, question_page.id, status=CallStatus.FAILED)

    rows = await find_confusion._fetch_recent_calls(tmp_db, limit=10)
    results = await find_confusion._score_heuristics(tmp_db, rows)

    flagged_ids = [r.call_id for r in results]
    assert bad.id in flagged_ids
    assert good.id not in flagged_ids
    bad_result = next(r for r in results if r.call_id == bad.id)
    assert any(s.name == "non_complete_status" for s in bad_result.signals)


async def test_score_heuristics_flags_trace_error(tmp_db, question_page):
    call = await _make_call(tmp_db, question_page.id)
    await _save_raw_trace(
        tmp_db,
        call.id,
        [{"event": "error", "message": "something went wrong"}],
    )

    rows = await find_confusion._fetch_recent_calls(tmp_db, limit=10)
    results = await find_confusion._score_heuristics(tmp_db, rows)

    flagged = next((r for r in results if r.call_id == call.id), None)
    assert flagged is not None
    assert any(s.name == "trace_error" for s in flagged.signals)


async def test_score_heuristics_skips_claude_code_direct(tmp_db, question_page):
    envelope = await _make_call(
        tmp_db,
        question_page.id,
        call_type=CallType.CLAUDE_CODE_DIRECT,
        status=CallStatus.PENDING,
    )

    rows = await find_confusion._fetch_recent_calls(tmp_db, limit=10)
    results = await find_confusion._score_heuristics(tmp_db, rows)

    assert envelope.id not in [r.call_id for r in results]


async def test_score_heuristics_flags_exchange_error(tmp_db, question_page):
    call = await _make_call(tmp_db, question_page.id)
    await tmp_db.save_llm_exchange(
        call_id=call.id,
        phase="update_workspace",
        system_prompt="sys",
        user_message="user",
        response_text=None,
        error="API timeout",
        round_num=1,
    )

    rows = await find_confusion._fetch_recent_calls(tmp_db, limit=10)
    results = await find_confusion._score_heuristics(tmp_db, rows)

    flagged = next((r for r in results if r.call_id == call.id), None)
    assert flagged is not None
    assert any(s.name == "exchange_error" for s in flagged.signals)


async def test_score_heuristics_thin_output(tmp_db, question_page):
    call = await _make_call(tmp_db, question_page.id)
    await tmp_db.save_llm_exchange(
        call_id=call.id,
        phase="update_workspace",
        system_prompt="sys",
        user_message="x" * 3000,
        response_text="y" * 50,
        round_num=1,
    )

    rows = await find_confusion._fetch_recent_calls(tmp_db, limit=10)
    results = await find_confusion._score_heuristics(tmp_db, rows)

    flagged = next((r for r in results if r.call_id == call.id), None)
    assert flagged is not None
    assert any(s.name == "thin_output" for s in flagged.signals)


async def test_score_heuristics_flags_cost_outlier(tmp_db, question_page):
    for _ in range(4):
        await _make_call(tmp_db, question_page.id, cost_usd=0.01)
    pricey = await _make_call(tmp_db, question_page.id, cost_usd=0.50)

    rows = await find_confusion._fetch_recent_calls(tmp_db, limit=20)
    results = await find_confusion._score_heuristics(tmp_db, rows)

    flagged = next((r for r in results if r.call_id == pricey.id), None)
    assert flagged is not None
    assert any(s.name == "cost_outlier" for s in flagged.signals)


async def test_score_heuristics_ranks_by_severity(tmp_db, question_page):
    mild = await _make_call(tmp_db, question_page.id, status=CallStatus.PENDING)
    severe = await _make_call(tmp_db, question_page.id, status=CallStatus.FAILED)
    await _save_raw_trace(
        tmp_db,
        severe.id,
        [{"event": "error", "message": "boom"}],
    )

    rows = await find_confusion._fetch_recent_calls(tmp_db, limit=10)
    results = await find_confusion._score_heuristics(tmp_db, rows)

    flagged_ids = [r.call_id for r in results]
    assert severe.id in flagged_ids
    assert mild.id in flagged_ids
    severe_idx = flagged_ids.index(severe.id)
    mild_idx = flagged_ids.index(mild.id)
    assert severe_idx < mild_idx


def test_heuristic_result_short_id():
    result = find_confusion.HeuristicResult(
        call_id="abcdef1234567890",
        call_type="assess",
        status="complete",
        cost_usd=0.01,
        created_at="2025-01-01T00:00:00",
        signals=[],
        score=0,
    )
    assert result.short_id == "abcdef12"


def test_median_even_and_odd():
    assert find_confusion._median([1.0, 2.0, 3.0]) == 2.0
    assert find_confusion._median([1.0, 2.0, 3.0, 4.0]) == 2.5
    assert find_confusion._median([]) == 0.0


def test_format_trace_for_llm_includes_exchange_content():
    call_row = {
        "id": "call-1",
        "call_type": "find_considerations",
        "status": "complete",
        "cost_usd": 0.02,
        "trace_json": [
            {"event": "context_built", "ts": "2025", "call_id": "call-1", "size": 42},
        ],
    }
    exchanges = [
        {
            "phase": "update_workspace",
            "round": 1,
            "user_message": "do the thing",
            "response_text": "here is the thing",
            "tool_calls": None,
        },
    ]
    rendered = find_confusion._format_trace_for_llm(call_row, exchanges)
    assert "call-1" in rendered
    assert "context_built" in rendered
    assert "do the thing" in rendered
    assert "here is the thing" in rendered


async def test_main_heuristic_prints_flags(capsys, monkeypatch, tmp_db, question_page):
    """Running main() without --deep prints heuristic results only."""
    call = await _make_call(tmp_db, question_page.id, status=CallStatus.FAILED)

    async def _fake_make_db(*, prod=False, staged=False, workspace=None, run_id=None):
        return tmp_db, "test-workspace"

    async def _noop_close():
        return None

    monkeypatch.setattr(find_confusion, "make_db", _fake_make_db)
    monkeypatch.setattr(tmp_db, "close", _noop_close)
    monkeypatch.setattr("sys.argv", ["find_confusion", "--limit", "10"])

    await find_confusion.main()
    out = capsys.readouterr().out

    assert "heuristic flags" in out
    assert call.id[:8] in out


async def test_main_structural_scan(capsys, monkeypatch, tmp_db, question_page):
    """--structural runs graph health checks without LLM."""

    async def _fake_make_db(*, prod=False, staged=False, workspace=None, run_id=None):
        return tmp_db, "test-workspace"

    async def _noop_close():
        return None

    monkeypatch.setattr(find_confusion, "make_db", _fake_make_db)
    monkeypatch.setattr(tmp_db, "close", _noop_close)
    monkeypatch.setattr(
        "sys.argv",
        ["find_confusion", "--structural", question_page.id],
    )

    await find_confusion.main()
    out = capsys.readouterr().out

    assert "structural health" in out
    assert question_page.id[:8] in out
