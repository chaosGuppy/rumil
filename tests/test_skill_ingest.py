"""Tests for rumil_skills.ingest_source — the source ingest skill.

Uses a pre-existing Source page via --from-page to skip the fetch step.
The ingest call exercises real Haiku through the rumil ingest pipeline.
"""

import pytest
import pytest_asyncio
from rumil_skills import _runctx, ingest_source

from rumil.models import (
    CallStatus,
    CallType,
    Page,
    PageLayer,
    PageType,
    Workspace,
)


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch, tmp_path):
    monkeypatch.setattr(_runctx, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(_runctx, "STATE_FILE", tmp_path / "state" / "rumil-session.json")


@pytest.fixture
def patch_make_db(monkeypatch, tmp_db):
    async def _fake_make_db(*, prod=False, staged=False, workspace=None, run_id=None):
        return tmp_db, "test-workspace"

    async def _noop_close():
        return None

    monkeypatch.setattr(ingest_source, "make_db", _fake_make_db)
    monkeypatch.setattr(tmp_db, "close", _noop_close)
    return tmp_db


@pytest_asyncio.fixture
async def source_page(tmp_db):
    page = Page(
        page_type=PageType.SOURCE,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=(
            "AI capability has improved rapidly. Cognitive tasks that "
            "previously took humans hours can now be done in seconds. "
            "White collar work such as drafting, summarizing, and basic "
            "analysis are increasingly performed by LLMs."
        ),
        headline="Source: notes on AI automation of cognitive work",
        extra={"filename": "ai-notes.txt", "char_count": 200},
    )
    await tmp_db.save_page(page)
    return page


@pytest.mark.llm
async def test_ingest_from_existing_source_runs_one_round(
    monkeypatch, patch_make_db, tmp_db, question_page, source_page
):
    """Ingest from an existing Source page runs ingest rounds end-to-end."""
    monkeypatch.setattr(
        "sys.argv",
        [
            "ingest_source",
            "--from-page",
            source_page.id,
            "--for",
            question_page.id,
            "--budget",
            "1",
            "--smoke-test",
        ],
    )
    await ingest_source.main()

    calls = await tmp_db._execute(
        tmp_db.client.table("calls")
        .select("*")
        .eq("call_type", CallType.INGEST.value)
        .eq("scope_page_id", source_page.id)
    )
    rows = list(getattr(calls, "data", None) or [])
    assert len(rows) >= 1

    non_pending = [r for r in rows if r["status"] != CallStatus.PENDING.value]
    assert len(non_pending) >= 1


@pytest.mark.integration
async def test_ingest_records_run_with_origin(
    monkeypatch, patch_make_db, tmp_db, question_page, source_page
):
    monkeypatch.setattr(
        "sys.argv",
        [
            "ingest_source",
            "--from-page",
            source_page.id,
            "--for",
            question_page.id,
            "--budget",
            "1",
            "--smoke-test",
        ],
    )
    await ingest_source.main()

    runs = await tmp_db._execute(tmp_db.client.table("runs").select("*").eq("id", tmp_db.run_id))
    rows = list(getattr(runs, "data", None) or [])
    assert len(rows) == 1
    config = rows[0].get("config") or {}
    assert config.get("origin") == "claude-code"
    assert config.get("skill") == "rumil-ingest"


async def test_ingest_no_source_or_page_exits(monkeypatch, patch_make_db, capsys):
    monkeypatch.setattr(
        "sys.argv",
        ["ingest_source", "--for", "somequestion"],
    )
    with pytest.raises(SystemExit) as excinfo:
        await ingest_source.main()
    assert excinfo.value.code == 2
    out = capsys.readouterr().out
    assert "must pass either" in out


async def test_ingest_both_source_and_from_page_exits(monkeypatch, patch_make_db, capsys):
    monkeypatch.setattr(
        "sys.argv",
        [
            "ingest_source",
            "some-file.txt",
            "--from-page",
            "abc",
            "--for",
            "somequestion",
        ],
    )
    with pytest.raises(SystemExit) as excinfo:
        await ingest_source.main()
    assert excinfo.value.code == 2
    out = capsys.readouterr().out
    assert "not both" in out


async def test_ingest_unknown_question_exits(
    monkeypatch, patch_make_db, tmp_db, source_page, capsys
):
    monkeypatch.setattr(
        "sys.argv",
        [
            "ingest_source",
            "--from-page",
            source_page.id,
            "--for",
            "deadbeef",
        ],
    )
    with pytest.raises(SystemExit) as excinfo:
        await ingest_source.main()
    assert excinfo.value.code == 1
    out = capsys.readouterr().out
    assert "deadbeef" in out


def test_is_url():
    assert ingest_source._is_url("http://example.com")
    assert ingest_source._is_url("https://example.com/path")
    assert not ingest_source._is_url("relative/file.txt")
    assert not ingest_source._is_url("/absolute/file.txt")
    assert not ingest_source._is_url("ftp://example.com")
