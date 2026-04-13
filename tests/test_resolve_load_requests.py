"""Tests for load_page move execution."""

from rumil.models import (
    Call,
    CallStatus,
    CallType,
    Page,
    PageLayer,
    PageType,
    Workspace,
)
from rumil.moves.load_page import LoadPagePayload, execute


async def _make_page(tmp_db):
    """Create a page in the DB and return it."""
    page = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="Test claim content",
        headline="Test claim",
    )
    await tmp_db.save_page(page)
    return page


def _dummy_call():
    return Call(
        call_type=CallType.FIND_CONSIDERATIONS,
        workspace=Workspace.RESEARCH,
        status=CallStatus.RUNNING,
    )


async def test_loads_page_by_short_id(tmp_db):
    page = await _make_page(tmp_db)
    result = await execute(
        LoadPagePayload(page_id=page.id[:8], detail="content"),
        _dummy_call(),
        tmp_db,
    )
    assert "Test claim content" in result.message


async def test_loads_page_by_full_id(tmp_db):
    page = await _make_page(tmp_db)
    result = await execute(
        LoadPagePayload(page_id=page.id, detail="content"),
        _dummy_call(),
        tmp_db,
    )
    assert "Test claim content" in result.message


async def test_returns_not_found_for_unknown_id(tmp_db):
    result = await execute(LoadPagePayload(page_id="nonexist"), _dummy_call(), tmp_db)
    assert "not found" in result.message


async def test_does_not_create_pages(tmp_db):
    await _make_page(tmp_db)
    result = await execute(LoadPagePayload(page_id="nonexist"), _dummy_call(), tmp_db)
    assert result.created_page_id is None
