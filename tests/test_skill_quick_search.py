"""Tests for the quick_search skill module (embedding-based)."""

from __future__ import annotations

import pytest

from rumil.embeddings import embed_and_store_page
from rumil.models import Page, PageLayer, PageType, Workspace
from rumil_skills import _runctx, quick_search


pytestmark = pytest.mark.llm


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch, tmp_path):
    monkeypatch.setattr(_runctx, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(
        _runctx, "STATE_FILE", tmp_path / "state" / "rumil-session.json"
    )


async def _noop_close():
    return None


@pytest.fixture
def patch_make_db(monkeypatch, tmp_db):
    async def _fake_make_db(*, prod=False, staged=False, workspace=None, run_id=None):
        return tmp_db, "test-workspace"

    monkeypatch.setattr(quick_search, "make_db", _fake_make_db)
    monkeypatch.setattr(tmp_db, "close", _noop_close)
    return tmp_db


async def test_quick_search_finds_seeded_claim(
    capsys, monkeypatch, patch_make_db, tmp_db
):
    page = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="Photosynthesis converts sunlight into chemical energy in plants.",
        headline="Photosynthesis converts light to energy",
    )
    await tmp_db.save_page(page)
    await embed_and_store_page(tmp_db, page)

    monkeypatch.setattr(
        "sys.argv", ["quick_search", "how", "plants", "use", "sunlight"]
    )
    await quick_search.main()
    out = capsys.readouterr().out

    assert "test-workspace" in out
    assert "results for:" in out
    assert page.id[:8] in out


async def test_quick_search_no_results(capsys, monkeypatch, patch_make_db):
    monkeypatch.setattr("sys.argv", ["quick_search", "completely", "unrelated"])
    await quick_search.main()
    out = capsys.readouterr().out

    assert "test-workspace" in out
    assert "No matching pages found" in out
