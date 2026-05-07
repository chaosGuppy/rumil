"""Tests for the trace skill module."""

import pytest
from rumil_skills import _runctx
from rumil_skills import trace as trace_mod


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

    monkeypatch.setattr(trace_mod, "make_db", _fake_make_db)
    monkeypatch.setattr(tmp_db, "close", _noop_close)
    return tmp_db


async def test_trace_prints_events_and_exchanges(
    capsys, monkeypatch, patch_make_db, tmp_db, scout_call
):
    await tmp_db.save_call_trace(
        scout_call.id,
        [
            {
                "event": "context_built",
                "ts": "2026-04-13T10:00:00",
                "working_context_page_ids": ["pA", "pB"],
                "preloaded_page_ids": ["pC"],
                "budget": 5,
            },
            {
                "event": "llm_exchange",
                "ts": "2026-04-13T10:00:05",
                "phase": "update_workspace",
                "round": 1,
                "input_tokens": 100,
                "output_tokens": 50,
                "duration_ms": 2000,
                "cost_usd": 0.01,
            },
        ],
    )
    await tmp_db.save_llm_exchange(
        call_id=scout_call.id,
        phase="update_workspace",
        system_prompt="PROMPT_SYS",
        user_message="PROMPT_USER",
        response_text="RESP_TEXT",
        input_tokens=100,
        output_tokens=50,
        round_num=1,
    )

    monkeypatch.setattr("sys.argv", ["trace", scout_call.id])
    await trace_mod.main()
    out = capsys.readouterr().out

    assert scout_call.id in out
    assert "trace events" in out
    assert "context_built" in out
    assert "llm_exchange" in out
    assert "llm exchanges (verbatim)" in out
    assert "PROMPT_SYS" in out
    assert "PROMPT_USER" in out
    assert "RESP_TEXT" in out


async def test_trace_short_id_resolution(capsys, monkeypatch, patch_make_db, tmp_db, scout_call):
    await tmp_db.save_call_trace(
        scout_call.id,
        [{"event": "context_built", "ts": "2026-04-13T10:00:00"}],
    )

    monkeypatch.setattr("sys.argv", ["trace", scout_call.id[:8]])
    await trace_mod.main()
    out = capsys.readouterr().out

    assert scout_call.id in out
    assert "context_built" in out


async def test_trace_unknown_call_exits(capsys, monkeypatch, patch_make_db):
    monkeypatch.setattr("sys.argv", ["trace", "ghostzzz"])
    with pytest.raises(SystemExit) as excinfo:
        await trace_mod.main()
    assert excinfo.value.code == 1
    out = capsys.readouterr().out
    assert "ghostzzz" in out


async def test_trace_brief_omits_system_prompt(
    capsys, monkeypatch, patch_make_db, tmp_db, scout_call
):
    await tmp_db.save_llm_exchange(
        call_id=scout_call.id,
        phase="update_workspace",
        system_prompt="SYSTEM_PROMPT_BODY_NOT_EXPECTED",
        user_message="user-words",
        response_text="resp-words",
        round_num=1,
    )

    monkeypatch.setattr("sys.argv", ["trace", scout_call.id, "--brief"])
    await trace_mod.main()
    out = capsys.readouterr().out

    assert "SYSTEM_PROMPT_BODY_NOT_EXPECTED" not in out
    assert "user-words" in out or "resp-words" in out


async def test_trace_only_filter(capsys, monkeypatch, patch_make_db, tmp_db, scout_call):
    await tmp_db.save_call_trace(
        scout_call.id,
        [
            {"event": "context_built", "ts": "2026-04-13T10:00:00"},
            {"event": "warning", "ts": "2026-04-13T10:00:01", "message": "WARN_X"},
        ],
    )

    monkeypatch.setattr(
        "sys.argv",
        ["trace", scout_call.id, "--only", "warning", "--no-exchanges"],
    )
    await trace_mod.main()
    out = capsys.readouterr().out

    assert "warning" in out
    assert "WARN_X" in out
    assert "context_built" not in out


async def test_trace_no_exchanges_flag(capsys, monkeypatch, patch_make_db, tmp_db, scout_call):
    await tmp_db.save_llm_exchange(
        call_id=scout_call.id,
        phase="update_workspace",
        system_prompt="sys",
        user_message="user",
        response_text="UNIQUE_RESP_MARKER",
        round_num=1,
    )

    monkeypatch.setattr("sys.argv", ["trace", scout_call.id, "--no-exchanges"])
    await trace_mod.main()
    out = capsys.readouterr().out

    assert "UNIQUE_RESP_MARKER" not in out
