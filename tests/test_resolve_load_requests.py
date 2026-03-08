"""Tests for resolve_load_requests with list[str] input."""

from differential.calls.common import resolve_load_requests
from differential.models import Page, PageLayer, PageType, Workspace


def _make_page(tmp_db):
    """Create a page in the DB and return it."""
    page = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="Test claim",
        summary="Test claim",
    )
    tmp_db.save_page(page)
    return page


def test_resolves_short_ids_via_map(tmp_db):
    """Short IDs in the map should resolve to full UUIDs."""
    page = _make_page(tmp_db)
    short_id = page.id[:8]
    short_id_map = {short_id: page.id}

    result = resolve_load_requests([short_id], short_id_map, tmp_db)
    assert result == [page.id]


def test_resolves_full_ids_directly(tmp_db):
    """Full UUIDs not in the map should still resolve if the page exists."""
    page = _make_page(tmp_db)

    result = resolve_load_requests([page.id], {}, tmp_db)
    assert result == [page.id]


def test_skips_unknown_ids(tmp_db):
    """IDs that don't resolve to any page should be skipped."""
    result = resolve_load_requests(["nonexistent"], {}, tmp_db)
    assert result == []


def test_deduplicates(tmp_db):
    """Duplicate short IDs should only resolve once."""
    page = _make_page(tmp_db)
    short_id = page.id[:8]
    short_id_map = {short_id: page.id}

    result = resolve_load_requests([short_id, short_id], short_id_map, tmp_db)
    assert result == [page.id]


def test_empty_input(tmp_db):
    """Empty input should return empty output."""
    result = resolve_load_requests([], {}, tmp_db)
    assert result == []
