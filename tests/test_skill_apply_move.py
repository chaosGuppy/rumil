"""Tests for rumil_skills.apply_move — cc-mediated move application."""

import json
import uuid

import pytest
import pytest_asyncio
from rumil_skills import _runctx, apply_move
from rumil_skills.apply_move import (
    TraceRecordError,
    apply_validated_move,
)

from rumil.database import DB
from rumil.models import (
    Call,
    CallType,
    LinkType,
    MoveType,
    Page,
    PageLayer,
    PageType,
    Workspace,
)


@pytest.fixture
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(_runctx, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(_runctx, "STATE_FILE", tmp_path / "state" / "rumil-session.json")


def _set_workspace(workspace: str) -> None:
    state = _runctx.load_session_state()
    state.workspace = workspace
    _runctx.save_session_state(state)


def _get_envelope_run_id() -> str:
    state = _runctx.load_session_state()
    assert state.chat_envelope is not None
    return state.chat_envelope["run_id"]


def _run_main(monkeypatch, argv: list[str]) -> None:
    monkeypatch.setattr("sys.argv", ["apply_move", *argv])


@pytest_asyncio.fixture
async def envelope_call(tmp_db):
    """Create a CLAUDE_CODE_DIRECT envelope call directly against tmp_db.

    Skips the full ensure_chat_envelope() flow so tests can exercise
    apply_validated_move in isolation.
    """
    call = Call(
        call_type=CallType.CLAUDE_CODE_DIRECT,
        workspace=Workspace.RESEARCH,
    )
    await tmp_db.save_call(call)
    return call


async def test_list_moves_prints_registry(_isolated_state, monkeypatch, capsys):
    _run_main(monkeypatch, ["--list"])
    await apply_move.main()
    out = capsys.readouterr().out
    assert "CREATE_CLAIM" in out
    assert "CREATE_QUESTION" in out


async def test_schema_renders_for_known_move(_isolated_state, monkeypatch, capsys):
    _run_main(monkeypatch, ["--schema", "CREATE_CLAIM"])
    await apply_move.main()
    out = capsys.readouterr().out
    assert "CREATE_CLAIM" in out
    assert "headline" in out
    assert "content" in out


async def test_invalid_json_payload_exits(_isolated_state, monkeypatch):
    _run_main(monkeypatch, ["CREATE_CLAIM", "not-json"])
    with pytest.raises(SystemExit) as excinfo:
        await apply_move.main()
    assert excinfo.value.code == 2


async def test_accreting_only_refuses_destructive(_isolated_state, monkeypatch):
    _run_main(
        monkeypatch,
        ["REMOVE_LINK", json.dumps({"link_id": "deadbeef"}), "--accreting-only"],
    )
    with pytest.raises(SystemExit) as excinfo:
        await apply_move.main()
    assert excinfo.value.code == 2


async def test_create_claim_end_to_end_via_cli(_isolated_state, monkeypatch, envelope_cleanup):
    """Full CLI path: argparse → envelope creation → move → trace event."""
    workspace = f"test-apply-{uuid.uuid4().hex[:8]}"
    _set_workspace(workspace)

    payload = json.dumps(
        {
            "headline": "Transformers scale with compute and data",
            "content": "Empirical scaling laws demonstrate log-linear loss improvements.",
            "credence": 7,
            "credence_reasoning": "Supported by multiple scaling-law papers.",
            "robustness": 3,
            "robustness_reasoning": "Well-replicated empirical finding.",
        }
    )
    _run_main(monkeypatch, ["CREATE_CLAIM", payload])
    await apply_move.main()

    envelope_run_id = _get_envelope_run_id()
    envelope_cleanup.append(envelope_run_id)

    db = await DB.create(run_id=envelope_run_id)
    try:
        state = _runctx.load_session_state()
        assert state.chat_envelope is not None
        envelope_call_id = state.chat_envelope["call_id"]

        page_rows = (
            await db._execute(
                db.client.table("pages")
                .select("*")
                .eq("provenance_call_id", envelope_call_id)
                .eq("page_type", PageType.CLAIM.value)
            )
        ).data
        assert len(page_rows) == 1
        assert page_rows[0]["headline"].startswith("Transformers scale")
        trace_events = await db.get_call_trace(envelope_call_id)
        assert any(ev.get("event") == "moves_executed" for ev in trace_events)
    finally:
        await db.close()


async def test_apply_validated_move_creates_claim(tmp_db, envelope_call):
    payload = {
        "headline": "Compute doubles every 6 months",
        "content": "Training compute has been doubling every ~6 months since 2012.",
        "credence": 6,
        "credence_reasoning": "Multiple empirical measurements since 2012.",
        "robustness": 2,
        "robustness_reasoning": "Depends on a handful of compute-tracker studies.",
    }
    result = await apply_validated_move(
        db=tmp_db,
        envelope_call=envelope_call,
        move_type=MoveType.CREATE_CLAIM,
        payload=payload,
    )
    assert result.created_page_id is not None

    page_rows = (
        await tmp_db._execute(
            tmp_db.client.table("pages").select("*").eq("id", result.created_page_id)
        )
    ).data
    assert len(page_rows) == 1
    assert page_rows[0]["headline"].startswith("Compute doubles")
    assert page_rows[0]["credence"] == 6
    assert page_rows[0]["provenance_call_id"] == envelope_call.id

    trace_events = await tmp_db.get_call_trace(envelope_call.id)
    assert any(ev.get("event") == "moves_executed" for ev in trace_events)


async def test_apply_validated_move_links_consideration(tmp_db, envelope_call):
    question = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="Does regulation slow AI deployment?",
        headline="Does regulation slow AI deployment?",
    )
    await tmp_db.save_page(question)

    claim_result = await apply_validated_move(
        db=tmp_db,
        envelope_call=envelope_call,
        move_type=MoveType.CREATE_CLAIM,
        payload={
            "headline": "Regulation follows deployment",
            "content": "Historical pattern: rules arrive after the tech is live.",
            "credence": 5,
            "credence_reasoning": "Broad historical pattern with exceptions.",
            "robustness": 2,
            "robustness_reasoning": "Based on a handful of well-known case studies.",
        },
    )
    assert claim_result.created_page_id is not None
    claim_id = claim_result.created_page_id

    await apply_validated_move(
        db=tmp_db,
        envelope_call=envelope_call,
        move_type=MoveType.LINK_CONSIDERATION,
        payload={
            "claim_id": claim_id,
            "question_id": question.id,
            "strength": 4.0,
            "reasoning": "Directly quantifies deployment pace.",
        },
    )

    links = await tmp_db.get_links_from(claim_id)
    consideration_links = [lk for lk in links if lk.link_type == LinkType.CONSIDERATION]
    assert len(consideration_links) == 1
    assert consideration_links[0].to_page_id == question.id
    assert consideration_links[0].strength == 4.0


async def test_apply_validated_move_supersede_records_mutation_event(tmp_db, envelope_call):
    old_result = await apply_validated_move(
        db=tmp_db,
        envelope_call=envelope_call,
        move_type=MoveType.CREATE_CLAIM,
        payload={
            "headline": "Frontier models cost $100M to train",
            "content": "Based on compute-cost estimates.",
            "credence": 5,
            "credence_reasoning": "Ballpark from rough compute accounting.",
            "robustness": 2,
            "robustness_reasoning": "Approximate; firm numbers are not public.",
        },
    )
    old_id = old_result.created_page_id
    assert old_id is not None

    await apply_validated_move(
        db=tmp_db,
        envelope_call=envelope_call,
        move_type=MoveType.CREATE_CLAIM,
        payload={
            "headline": "Frontier models now cost $1B+ to train",
            "content": "Updated estimates show costs crossing the billion-dollar threshold.",
            "credence": 6,
            "credence_reasoning": "Updated estimates from more recent reporting.",
            "robustness": 2,
            "robustness_reasoning": "Still approximate; firm-costed is non-public.",
            "supersedes": old_id,
            "change_magnitude": 3,
        },
    )

    old_page = await tmp_db.get_page(old_id)
    assert old_page is not None
    assert old_page.is_superseded is True
    assert old_page.superseded_by is not None

    events = (
        await tmp_db._execute(
            tmp_db.client.table("mutation_events")
            .select("event_type, target_id")
            .eq("target_id", old_id)
            .eq("event_type", "supersede_page")
        )
    ).data
    assert len(events) >= 1


async def test_apply_validated_move_unknown_move_raises(tmp_db, envelope_call):
    fake = object()
    with pytest.raises(ValueError, match="no MoveDef"):
        await apply_validated_move(
            db=tmp_db,
            envelope_call=envelope_call,
            move_type=fake,  # type: ignore[arg-type]
            payload={},
        )


async def test_apply_validated_move_raises_trace_record_error(tmp_db, envelope_call, mocker):
    """If the trace write fails, the move has already landed but we fail loud."""
    mocker.patch.object(
        tmp_db,
        "save_call_trace",
        side_effect=RuntimeError("simulated trace write failure"),
    )
    payload = {
        "headline": "Trace write will fail",
        "content": "We still expect the page to be created before the raise.",
        "credence": 5,
        "credence_reasoning": "Test placeholder.",
        "robustness": 2,
        "robustness_reasoning": "Test placeholder.",
    }
    with pytest.raises(TraceRecordError, match="simulated trace write failure"):
        await apply_validated_move(
            db=tmp_db,
            envelope_call=envelope_call,
            move_type=MoveType.CREATE_CLAIM,
            payload=payload,
        )

    page_rows = (
        await tmp_db._execute(
            tmp_db.client.table("pages")
            .select("id")
            .eq("provenance_call_id", envelope_call.id)
            .eq("page_type", PageType.CLAIM.value)
        )
    ).data
    assert len(page_rows) == 1


async def test_main_exits_nonzero_when_trace_write_fails(
    _isolated_state, monkeypatch, envelope_cleanup, mocker, capsys
):
    """CLI surfaces trace-write failures as exit 1 with a clear error message."""
    workspace = f"test-apply-{uuid.uuid4().hex[:8]}"
    _set_workspace(workspace)

    mocker.patch.object(
        apply_move,
        "apply_validated_move",
        side_effect=TraceRecordError("boom"),
    )

    payload = json.dumps(
        {
            "headline": "Trace failure path",
            "content": "We expect exit 1 and a loud message.",
            "credence": 5,
            "credence_reasoning": "Test placeholder.",
            "robustness": 2,
            "robustness_reasoning": "Test placeholder.",
        }
    )
    _run_main(monkeypatch, ["CREATE_CLAIM", payload])
    with pytest.raises(SystemExit) as excinfo:
        await apply_move.main()
    assert excinfo.value.code == 1

    envelope_run_id = _get_envelope_run_id()
    envelope_cleanup.append(envelope_run_id)

    err = capsys.readouterr().err
    assert "move applied to DB but trace event failed to record" in err
    assert "boom" in err
    assert "envelope may be incomplete" in err
    assert envelope_run_id in err
