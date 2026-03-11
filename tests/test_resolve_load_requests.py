"""Tests for load_page move execution."""

from differential.models import (
    Call,
    CallStatus,
    CallType,
    Page,
    PageLayer,
    PageType,
    Workspace,
)
from differential.moves.base import MoveState
from differential.moves.load_page import LoadPagePayload, execute


async def _make_page(tmp_db):
    """Create a page in the DB and return it."""
    page = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="Test claim content",
        summary="Test claim",
    )
    await tmp_db.save_page(page)
    return page


def _dummy_state(tmp_db):
    call = Call(
        call_type=CallType.SCOUT,
        workspace=Workspace.RESEARCH,
        status=CallStatus.RUNNING,
    )
    return MoveState(call, tmp_db)


async def test_loads_page_by_short_id(tmp_db):
    page = await _make_page(tmp_db)
    result = await execute(LoadPagePayload(page_id=page.id[:8]), _dummy_state(tmp_db))
    assert "Test claim content" in result.message


async def test_loads_page_by_full_id(tmp_db):
    page = await _make_page(tmp_db)
    result = await execute(LoadPagePayload(page_id=page.id), _dummy_state(tmp_db))
    assert "Test claim content" in result.message


async def test_returns_not_found_for_unknown_id(tmp_db):
    result = await execute(LoadPagePayload(page_id="nonexist"), _dummy_state(tmp_db))
    assert "not found" in result.message


async def test_does_not_create_pages(tmp_db):
    await _make_page(tmp_db)
    result = await execute(LoadPagePayload(page_id="nonexist"), _dummy_state(tmp_db))
    assert result.created_page_id is None
