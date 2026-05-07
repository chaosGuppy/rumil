"""Tests for the show_question skill module."""

import pytest
from rumil_skills import _runctx, show_question

from rumil.models import LinkType, Page, PageLayer, PageLink, PageType, Workspace


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

    monkeypatch.setattr(show_question, "make_db", _fake_make_db)
    monkeypatch.setattr(tmp_db, "close", _noop_close)
    return tmp_db


async def test_show_question_prints_id_and_headline(
    capsys, monkeypatch, patch_make_db, question_page
):
    monkeypatch.setattr(
        "sys.argv",
        ["show_question", question_page.id, "--no-neighbors", "--no-calls"],
    )
    await show_question.main()
    out = capsys.readouterr().out

    assert question_page.id[:8] in out
    assert "frontier AI" in out  # a word from the question headline
    assert "test-workspace" in out
    assert "research subtree" in out


async def test_show_question_with_short_id(capsys, monkeypatch, patch_make_db, question_page):
    short_id = question_page.id[:8]
    monkeypatch.setattr("sys.argv", ["show_question", short_id, "--no-neighbors", "--no-calls"])
    await show_question.main()
    out = capsys.readouterr().out

    assert short_id in out
    assert "frontier AI" in out


async def test_show_question_unknown_id_exits(capsys, monkeypatch, patch_make_db):
    monkeypatch.setattr(
        "sys.argv",
        ["show_question", "deadbeef", "--no-neighbors", "--no-calls"],
    )
    with pytest.raises(SystemExit) as excinfo:
        await show_question.main()
    assert excinfo.value.code == 1
    out = capsys.readouterr().out
    assert "deadbeef" in out


async def test_show_question_recent_calls_section(
    capsys, monkeypatch, patch_make_db, question_page, scout_call
):
    monkeypatch.setattr("sys.argv", ["show_question", question_page.id, "--no-neighbors"])
    await show_question.main()
    out = capsys.readouterr().out

    assert "recent calls" in out
    assert scout_call.id[:8] in out


async def test_show_question_subtree_includes_child_question(
    capsys, monkeypatch, patch_make_db, tmp_db, question_page
):
    child = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="Child question body",
        headline="A distinctive child headline token CHILDQX",
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
        ["show_question", question_page.id, "--no-neighbors", "--no-calls"],
    )
    await show_question.main()
    out = capsys.readouterr().out

    assert "CHILDQX" in out
