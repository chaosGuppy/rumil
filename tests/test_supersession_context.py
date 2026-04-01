"""Tests for supersession chain resolution and format_page annotation."""

import pytest
import pytest_asyncio

from rumil.context import format_page
from rumil.models import (
    Page,
    PageDetail,
    PageLayer,
    PageType,
    Workspace,
)
from rumil.page_graph import PageGraph


def _claim(headline: str = "A claim", **overrides) -> Page:
    defaults = {
        "page_type": PageType.CLAIM,
        "layer": PageLayer.SQUIDGY,
        "workspace": Workspace.RESEARCH,
        "content": f"Full content of: {headline}",
        "headline": headline,
        "abstract": f"Abstract of: {headline}",
        "credence": 6,
        "robustness": 3,
    }
    defaults.update(overrides)
    return Page(**defaults)


@pytest_asyncio.fixture
async def chain_abc(tmp_db):
    """Create a three-page chain: A -> B -> C (C is active)."""
    a = _claim("Claim A")
    b = _claim("Claim B")
    c = _claim("Claim C")
    for p in (a, b, c):
        await tmp_db.save_page(p)
    await tmp_db.supersede_page(a.id, b.id)
    await tmp_db.supersede_page(b.id, c.id)
    a = await tmp_db.get_page(a.id)
    b = await tmp_db.get_page(b.id)
    c = await tmp_db.get_page(c.id)
    return a, b, c


async def test_resolve_single_hop(tmp_db):
    old = _claim("Old")
    new = _claim("New")
    await tmp_db.save_page(old)
    await tmp_db.save_page(new)
    await tmp_db.supersede_page(old.id, new.id)

    result = await tmp_db.resolve_supersession_chain(old.id)
    assert result is not None
    assert result.id == new.id


async def test_resolve_multi_hop(tmp_db, chain_abc):
    a, _b, c = chain_abc
    result = await tmp_db.resolve_supersession_chain(a.id)
    assert result is not None
    assert result.id == c.id


async def test_resolve_from_middle_of_chain(tmp_db, chain_abc):
    _a, b, c = chain_abc
    result = await tmp_db.resolve_supersession_chain(b.id)
    assert result is not None
    assert result.id == c.id


async def test_resolve_active_page_returns_none(tmp_db):
    active = _claim("Active")
    await tmp_db.save_page(active)
    result = await tmp_db.resolve_supersession_chain(active.id)
    assert result is None


async def test_resolve_broken_chain(tmp_db):
    old = _claim("Old")
    await tmp_db.save_page(old)
    await tmp_db.supersede_page(old.id, "nonexistent-id")

    result = await tmp_db.resolve_supersession_chain(old.id)
    assert result is None


async def test_resolve_max_depth(tmp_db):
    pages = [_claim(f"Page {i}") for i in range(5)]
    for p in pages:
        await tmp_db.save_page(p)
    for i in range(4):
        await tmp_db.supersede_page(pages[i].id, pages[i + 1].id)

    result = await tmp_db.resolve_supersession_chain(pages[0].id, max_depth=2)
    assert result is None


async def test_format_page_active_no_annotation(tmp_db):
    page = _claim("Active claim")
    await tmp_db.save_page(page)

    text = await format_page(page, PageDetail.CONTENT, db=tmp_db, linked_detail=None)
    assert "SUPERSEDED" not in text
    assert "Active claim" in text


async def test_format_page_headline_superseded(tmp_db):
    old = _claim("Old claim")
    new = _claim("New claim")
    await tmp_db.save_page(old)
    await tmp_db.save_page(new)
    await tmp_db.supersede_page(old.id, new.id)

    old = await tmp_db.get_page(old.id)
    text = await format_page(old, PageDetail.HEADLINE, db=tmp_db, linked_detail=None)
    assert "[SUPERSEDED]" in text
    assert old.id[:8] in text
    assert "New claim" in text


async def test_format_page_abstract_superseded(tmp_db):
    old = _claim("Old claim")
    new = _claim("New claim")
    await tmp_db.save_page(old)
    await tmp_db.save_page(new)
    await tmp_db.supersede_page(old.id, new.id)

    old = await tmp_db.get_page(old.id)
    text = await format_page(old, PageDetail.ABSTRACT, db=tmp_db, linked_detail=None)
    assert "SUPERSEDED" in text
    assert "Abstract of: Old claim" in text
    assert "Abstract of: New claim" in text
    assert new.id[:8] in text


async def test_format_page_content_superseded(tmp_db):
    old = _claim("Old claim")
    new = _claim("New claim")
    await tmp_db.save_page(old)
    await tmp_db.save_page(new)
    await tmp_db.supersede_page(old.id, new.id)

    old = await tmp_db.get_page(old.id)
    text = await format_page(old, PageDetail.CONTENT, db=tmp_db, linked_detail=None)
    assert "SUPERSEDED" in text
    assert "Full content of: Old claim" in text
    assert "Full content of: New claim" in text


async def test_format_page_superseded_chain_resolves_to_end(tmp_db, chain_abc):
    a, _b, c = chain_abc
    text = await format_page(a, PageDetail.CONTENT, db=tmp_db, linked_detail=None)
    assert "SUPERSEDED" in text
    assert "Full content of: Claim A" in text
    assert "Full content of: Claim C" in text
    assert c.id[:8] in text


async def test_format_page_broken_chain(tmp_db):
    old = _claim("Orphaned claim")
    await tmp_db.save_page(old)
    await tmp_db.supersede_page(old.id, "nonexistent-id")

    old = await tmp_db.get_page(old.id)
    text = await format_page(old, PageDetail.CONTENT, db=tmp_db, linked_detail=None)
    assert "SUPERSEDED" in text
    assert "replacement not found" in text
    assert "Full content of: Orphaned claim" in text


async def test_format_page_include_superseding_false(tmp_db):
    old = _claim("Old claim")
    new = _claim("New claim")
    await tmp_db.save_page(old)
    await tmp_db.save_page(new)
    await tmp_db.supersede_page(old.id, new.id)

    old = await tmp_db.get_page(old.id)
    text = await format_page(
        old, PageDetail.CONTENT, db=tmp_db, linked_detail=None,
        include_superseding=False,
    )
    assert "SUPERSEDED" not in text
    assert "Full content of: Old claim" in text


async def test_page_graph_resolve_single_hop():
    old = _claim("Old")
    new = _claim("New")
    old_superseded = old.model_copy(update={
        "is_superseded": True,
        "superseded_by": new.id,
    })
    graph = PageGraph(pages=[new], links=[])
    result = await graph.resolve_supersession_chain(old_superseded)
    assert result is not None
    assert result.id == new.id


async def test_page_graph_returns_none_for_active_page():
    active = _claim("Active")
    graph = PageGraph(pages=[active], links=[])
    result = await graph.resolve_supersession_chain(active)
    assert result is None


async def test_page_graph_returns_none_when_target_missing():
    old = _claim("Old")
    old_superseded = old.model_copy(update={
        "is_superseded": True,
        "superseded_by": "missing-id",
    })
    graph = PageGraph(pages=[], links=[])
    result = await graph.resolve_supersession_chain(old_superseded)
    assert result is None
