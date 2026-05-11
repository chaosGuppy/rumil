"""Tests for the search_workspace skill module (embedding-based)."""

import pytest
from rumil_skills import _runctx, search_workspace

from rumil.embeddings import embed_and_store_page
from rumil.models import Page, PageLayer, PageType, Workspace

pytestmark = pytest.mark.llm


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

    monkeypatch.setattr(search_workspace, "make_db", _fake_make_db)
    monkeypatch.setattr(tmp_db, "close", _noop_close)
    return tmp_db


async def test_quick_mode_finds_seeded_claim(capsys, monkeypatch, patch_make_db, tmp_db):
    page = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="Photosynthesis converts sunlight into chemical energy in plants.",
        headline="Photosynthesis converts light to energy",
    )
    await tmp_db.save_page(page)
    await embed_and_store_page(tmp_db, page)

    monkeypatch.setattr("sys.argv", ["search_workspace", "how", "plants", "use", "sunlight"])
    await search_workspace.main()
    out = capsys.readouterr().out

    assert "test-workspace" in out
    assert "results for:" in out
    assert page.id[:8] in out


async def test_quick_mode_no_results(capsys, monkeypatch, patch_make_db):
    monkeypatch.setattr("sys.argv", ["search_workspace", "completely", "unrelated"])
    await search_workspace.main()
    out = capsys.readouterr().out

    assert "test-workspace" in out
    assert "No matching pages found" in out


async def test_full_mode_finds_seeded_page(capsys, monkeypatch, patch_make_db, tmp_db):
    page = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="The mitochondria is the powerhouse of the cell.",
        headline="Mitochondria generate cellular energy",
        abstract="Mitochondria are cellular organelles that generate ATP energy.",
    )
    await tmp_db.save_page(page)
    # build_embedding_based_context searches on the abstract field.
    await embed_and_store_page(tmp_db, page, field_name="abstract")

    monkeypatch.setattr("sys.argv", ["search_workspace", "--full", "cellular", "energy", "source"])
    await search_workspace.main()
    out = capsys.readouterr().out

    assert "test-workspace" in out
    assert "cellular" in out
    assert page.id[:8] in out


async def test_full_mode_empty(capsys, monkeypatch, patch_make_db):
    monkeypatch.setattr("sys.argv", ["search_workspace", "--full", "obscure", "topic", "nothing"])
    await search_workspace.main()
    out = capsys.readouterr().out

    assert "test-workspace" in out
    assert "obscure" in out
