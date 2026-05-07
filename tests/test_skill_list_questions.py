"""Tests for the list_questions skill module."""

import pytest
import pytest_asyncio
from rumil_skills import _runctx, list_questions

from rumil.models import Page, PageLayer, PageType, Workspace


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch, tmp_path):
    monkeypatch.setattr(_runctx, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(_runctx, "STATE_FILE", tmp_path / "state" / "rumil-session.json")


@pytest.fixture
def patch_make_db(monkeypatch, tmp_db):
    async def _fake_make_db(*, prod=False, staged=False, workspace=None, run_id=None):
        return tmp_db, "test-workspace"

    monkeypatch.setattr(list_questions, "make_db", _fake_make_db)
    monkeypatch.setattr(tmp_db, "close", _noop_close)
    return tmp_db


async def _noop_close():
    return None


@pytest_asyncio.fixture
async def seeded_questions(tmp_db):
    q1 = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="First root question content",
        headline="First root question headline",
    )
    q2 = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="Second root question content",
        headline="Second root question headline",
    )
    await tmp_db.save_page(q1)
    await tmp_db.save_page(q2)
    return [q1, q2]


async def test_list_questions_prints_workspace_and_each_headline(
    capsys, monkeypatch, patch_make_db, seeded_questions
):
    monkeypatch.setattr("sys.argv", ["list_questions"])
    await list_questions.main()
    out = capsys.readouterr().out

    assert "test-workspace" in out
    assert "First root question headline" in out
    assert "Second root question headline" in out
    for q in seeded_questions:
        assert q.id[:8] in out


async def test_list_questions_empty(capsys, monkeypatch, patch_make_db):
    monkeypatch.setattr("sys.argv", ["list_questions"])
    await list_questions.main()
    out = capsys.readouterr().out

    assert "test-workspace" in out
    assert "no root questions" in out
