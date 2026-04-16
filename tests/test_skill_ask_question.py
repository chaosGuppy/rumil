"""Tests for rumil_skills.ask_question — cc-mediated question creation."""

from __future__ import annotations

import json
import uuid

import pytest
import pytest_asyncio

from rumil.database import DB
from rumil.models import (
    LinkType,
    Page,
    PageLayer,
    PageType,
    Workspace,
)
from rumil_skills import _runctx, ask_question


@pytest.fixture
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(_runctx, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(
        _runctx, "STATE_FILE", tmp_path / "state" / "rumil-session.json"
    )


@pytest_asyncio.fixture
async def envelope_cleanup():
    """Track envelope run_ids so we can tear down every row they wrote."""
    run_ids: list[str] = []

    yield run_ids

    project_ids: set[str] = set()
    for run_id in reversed(run_ids):
        cleanup_db = await DB.create(run_id=run_id)
        try:
            rows = (
                await cleanup_db._execute(
                    cleanup_db.client.table("runs")
                    .select("project_id")
                    .eq("id", run_id)
                )
            ).data
            if rows and rows[0].get("project_id"):
                project_ids.add(rows[0]["project_id"])
            await cleanup_db.delete_run_data()
        finally:
            await cleanup_db.close()
    if project_ids:
        cleanup_db = await DB.create(run_id="cleanup")
        try:
            for pid in project_ids:
                await cleanup_db._execute(
                    cleanup_db.client.table("projects").delete().eq("id", pid)
                )
        finally:
            await cleanup_db.close()


def _set_workspace(workspace: str) -> None:
    state = _runctx.load_session_state()
    state.workspace = workspace
    _runctx.save_session_state(state)


async def _seed_parent_question(workspace: str) -> tuple[str, str]:
    """Create a parent QUESTION page under its own run. Returns (qid, run_id)."""
    seed_run_id = str(uuid.uuid4())
    db = await DB.create(run_id=seed_run_id)
    project = await db.get_or_create_project(workspace)
    db.project_id = project.id
    await db.init_budget(10)
    await db.create_run(name="seed", question_id=None, config={})
    page = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="How fast are compute costs falling?",
        headline="How fast are compute costs falling?",
    )
    await db.save_page(page)
    await db.close()
    return page.id, seed_run_id


async def _seed_non_question(workspace: str) -> tuple[str, str]:
    seed_run_id = str(uuid.uuid4())
    db = await DB.create(run_id=seed_run_id)
    project = await db.get_or_create_project(workspace)
    db.project_id = project.id
    await db.init_budget(10)
    await db.create_run(name="seed", question_id=None, config={})
    page = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="Some claim body.",
        headline="A claim, not a question.",
    )
    await db.save_page(page)
    await db.close()
    return page.id, seed_run_id


def _get_envelope_run_id() -> str:
    state = _runctx.load_session_state()
    assert state.chat_envelope is not None
    return state.chat_envelope["run_id"]


def _run_main(monkeypatch, argv: list[str]) -> None:
    monkeypatch.setattr("sys.argv", ["ask_question", *argv])


async def test_parse_question_input_plain_string():
    q = ask_question.parse_question_input("Is the sky blue?")
    assert q.headline == "Is the sky blue?"
    assert q.abstract == ""
    assert q.content == ""


async def test_parse_question_input_json_file(tmp_path):
    path = tmp_path / "q.json"
    path.write_text(
        json.dumps(
            {
                "headline": "Does regulation slow deployment?",
                "abstract": "A short summary.",
                "content": "Longer content here.",
            }
        )
    )
    q = ask_question.parse_question_input(str(path))
    assert q.headline == "Does regulation slow deployment?"
    assert q.abstract == "A short summary."
    assert q.content == "Longer content here."


async def test_parse_question_input_json_missing_headline(tmp_path):
    path = tmp_path / "q.json"
    path.write_text(json.dumps({"abstract": "No headline here."}))
    with pytest.raises(SystemExit):
        ask_question.parse_question_input(str(path))


async def test_parse_question_input_json_unknown_fields(tmp_path):
    path = tmp_path / "q.json"
    path.write_text(json.dumps({"headline": "hi", "surprise": "field"}))
    with pytest.raises(SystemExit):
        ask_question.parse_question_input(str(path))


async def test_root_question_creates_page(
    _isolated_state, monkeypatch, envelope_cleanup
):
    workspace = f"test-ask-{uuid.uuid4().hex[:8]}"
    _set_workspace(workspace)

    _run_main(
        monkeypatch,
        ["What shapes transformer sample efficiency?", "--abstract", "Short summary."],
    )
    await ask_question.main()

    envelope_run_id = _get_envelope_run_id()
    envelope_cleanup.append(envelope_run_id)

    db = await DB.create(run_id=envelope_run_id)
    try:
        state = _runctx.load_session_state()
        assert state.chat_envelope is not None
        envelope_call_id = state.chat_envelope["call_id"]

        rows = (
            await db._execute(
                db.client.table("pages")
                .select("*")
                .eq("provenance_call_id", envelope_call_id)
                .eq("page_type", PageType.QUESTION.value)
            )
        ).data
        assert len(rows) == 1
        assert rows[0]["headline"] == "What shapes transformer sample efficiency?"
        assert rows[0]["provenance_model"] == "human"
        assert rows[0]["abstract"] == "Short summary."

        links = (
            await db._execute(
                db.client.table("page_links")
                .select("*")
                .eq("to_page_id", rows[0]["id"])
            )
        ).data
        assert links == []
    finally:
        await db.close()


async def test_subquestion_links_to_parent(
    _isolated_state, monkeypatch, envelope_cleanup
):
    workspace = f"test-ask-{uuid.uuid4().hex[:8]}"
    _set_workspace(workspace)

    parent_id, seed_run_id = await _seed_parent_question(workspace)
    envelope_cleanup.append(seed_run_id)

    _run_main(
        monkeypatch,
        [
            "Is Moore's law still the right frame for training compute?",
            "--parent",
            parent_id[:8],
        ],
    )
    await ask_question.main()

    envelope_run_id = _get_envelope_run_id()
    envelope_cleanup.append(envelope_run_id)

    db = await DB.create(run_id=envelope_run_id)
    try:
        state = _runctx.load_session_state()
        assert state.chat_envelope is not None
        envelope_call_id = state.chat_envelope["call_id"]
        rows = (
            await db._execute(
                db.client.table("pages")
                .select("*")
                .eq("provenance_call_id", envelope_call_id)
                .eq("page_type", PageType.QUESTION.value)
            )
        ).data
        assert len(rows) == 1
        child_id = rows[0]["id"]

        link_rows = (
            await db._execute(
                db.client.table("page_links")
                .select("*")
                .eq("from_page_id", parent_id)
                .eq("to_page_id", child_id)
            )
        ).data
        assert len(link_rows) == 1
        assert link_rows[0]["link_type"] == LinkType.CHILD_QUESTION.value

        parent_page = await db.get_page(parent_id)
        assert parent_page is not None
        assert parent_page.page_type == PageType.QUESTION
    finally:
        await db.close()


async def test_subquestion_with_unknown_parent_exits_without_creating(
    _isolated_state, monkeypatch, envelope_cleanup, capsys
):
    workspace = f"test-ask-{uuid.uuid4().hex[:8]}"
    _set_workspace(workspace)

    _run_main(
        monkeypatch,
        ["Is this going anywhere?", "--parent", "deadbeef"],
    )
    with pytest.raises(SystemExit) as excinfo:
        await ask_question.main()
    assert excinfo.value.code == 1

    envelope_run_id = _get_envelope_run_id()
    envelope_cleanup.append(envelope_run_id)

    db = await DB.create(run_id=envelope_run_id)
    try:
        state = _runctx.load_session_state()
        assert state.chat_envelope is not None
        envelope_call_id = state.chat_envelope["call_id"]
        rows = (
            await db._execute(
                db.client.table("pages")
                .select("id")
                .eq("provenance_call_id", envelope_call_id)
            )
        ).data
        assert rows == []
    finally:
        await db.close()


async def test_subquestion_rejects_non_question_parent(
    _isolated_state, monkeypatch, envelope_cleanup
):
    workspace = f"test-ask-{uuid.uuid4().hex[:8]}"
    _set_workspace(workspace)

    claim_id, seed_run_id = await _seed_non_question(workspace)
    envelope_cleanup.append(seed_run_id)

    _run_main(
        monkeypatch,
        ["Will this resolve?", "--parent", claim_id[:8]],
    )
    with pytest.raises(SystemExit) as excinfo:
        await ask_question.main()
    assert excinfo.value.code == 1

    envelope_run_id = _get_envelope_run_id()
    envelope_cleanup.append(envelope_run_id)

    db = await DB.create(run_id=envelope_run_id)
    try:
        state = _runctx.load_session_state()
        assert state.chat_envelope is not None
        envelope_call_id = state.chat_envelope["call_id"]
        rows = (
            await db._execute(
                db.client.table("pages")
                .select("id")
                .eq("provenance_call_id", envelope_call_id)
            )
        ).data
        assert rows == []
    finally:
        await db.close()


async def test_content_defaults_to_headline_when_no_content_or_abstract(
    _isolated_state, monkeypatch, envelope_cleanup
):
    workspace = f"test-ask-{uuid.uuid4().hex[:8]}"
    _set_workspace(workspace)

    _run_main(monkeypatch, ["Minimal headline only"])
    await ask_question.main()

    envelope_run_id = _get_envelope_run_id()
    envelope_cleanup.append(envelope_run_id)

    db = await DB.create(run_id=envelope_run_id)
    try:
        state = _runctx.load_session_state()
        assert state.chat_envelope is not None
        envelope_call_id = state.chat_envelope["call_id"]
        rows = (
            await db._execute(
                db.client.table("pages")
                .select("*")
                .eq("provenance_call_id", envelope_call_id)
            )
        ).data
        assert len(rows) == 1
        assert rows[0]["content"] == "Minimal headline only"
    finally:
        await db.close()
