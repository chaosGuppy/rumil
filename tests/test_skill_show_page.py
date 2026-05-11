"""Tests for the show_page skill module."""

import pytest
import pytest_asyncio
from rumil_skills import _runctx, show_page

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

    monkeypatch.setattr(show_page, "make_db", _fake_make_db)
    monkeypatch.setattr(tmp_db, "close", _noop_close)
    return tmp_db


@pytest_asyncio.fixture
async def claim_page(tmp_db):
    page = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="The earth orbits the sun due to gravitational attraction.",
        headline="Earth orbits sun via gravity",
        credence=8,
        robustness=4,
    )
    await tmp_db.save_page(page)
    return page


async def test_show_page_prints_core_fields(capsys, monkeypatch, patch_make_db, claim_page):
    monkeypatch.setattr("sys.argv", ["show_page", claim_page.id, "--no-links"])
    await show_page.main()
    out = capsys.readouterr().out

    assert claim_page.id in out
    assert "Earth orbits sun via gravity" in out
    assert "claim" in out
    assert "gravitational attraction" in out
    assert "credence=8" in out
    assert "robustness=4" in out


async def test_show_page_short_id_resolution(capsys, monkeypatch, patch_make_db, claim_page):
    short_id = claim_page.id[:8]
    monkeypatch.setattr("sys.argv", ["show_page", short_id, "--no-links"])
    await show_page.main()
    out = capsys.readouterr().out

    assert claim_page.id in out
    assert short_id in out


async def test_show_page_missing_id_exits(capsys, monkeypatch, patch_make_db):
    monkeypatch.setattr("sys.argv", ["show_page", "nomatchx", "--no-links"])
    with pytest.raises(SystemExit) as excinfo:
        await show_page.main()
    assert excinfo.value.code == 1
    out = capsys.readouterr().out
    assert "nomatchx" in out


async def test_show_page_links_sections(
    capsys, monkeypatch, patch_make_db, tmp_db, question_page, claim_page
):
    await tmp_db.save_link(
        PageLink(
            from_page_id=claim_page.id,
            to_page_id=question_page.id,
            link_type=LinkType.CONSIDERATION,
        )
    )

    monkeypatch.setattr("sys.argv", ["show_page", claim_page.id])
    await show_page.main()
    out = capsys.readouterr().out

    assert "outgoing links" in out
    assert "incoming links" in out
    assert question_page.id[:8] in out
    assert "consideration" in out


async def test_show_page_content_truncation(capsys, monkeypatch, patch_make_db, tmp_db):
    long_body = "A" * 500
    page = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=long_body,
        headline="long page",
    )
    await tmp_db.save_page(page)

    monkeypatch.setattr(
        "sys.argv",
        ["show_page", page.id, "--no-links", "--content-limit", "100"],
    )
    await show_page.main()
    out = capsys.readouterr().out

    assert "truncated at 100 chars" in out
    assert "A" * 100 in out
