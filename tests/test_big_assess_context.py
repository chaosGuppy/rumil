"""Tests for BigAssessContext and its helper functions."""

import pytest
import pytest_asyncio

from rumil.calls.context_builders import (
    _gather_connected_pages,
    _get_latest_judgement,
    _cites_superseded_pages,
    _resolve_superseded_connections,
    _swap_superseded_link,
)
from rumil.models import (
    ConsiderationDirection,
    LinkType,
    Page,
    PageLayer,
    PageLink,
    PageType,
    Workspace,
)


def _page(page_type: PageType, headline: str, **overrides) -> Page:
    defaults = {
        "page_type": page_type,
        "layer": PageLayer.SQUIDGY,
        "workspace": Workspace.RESEARCH,
        "content": f"Content for {headline}",
        "headline": headline,
        "abstract": f"Abstract of {headline}",
    }
    defaults.update(overrides)
    return Page(**defaults)


def _question(headline: str = "Test question", **kw) -> Page:
    return _page(PageType.QUESTION, headline, **kw)


def _claim(headline: str = "Test claim", **kw) -> Page:
    return _page(PageType.CLAIM, headline, credence=6, robustness=3, **kw)


def _judgement(headline: str = "Test judgement", **kw) -> Page:
    return _page(PageType.JUDGEMENT, headline, **kw)


@pytest_asyncio.fixture
async def question_with_considerations(tmp_db):
    """A question with two consideration claims and one child question."""
    q = _question("Main question")
    c1 = _claim("Claim A")
    c2 = _claim("Claim B")
    child = _question("Sub-question")
    await tmp_db.save_page(q)
    await tmp_db.save_page(c1)
    await tmp_db.save_page(c2)
    await tmp_db.save_page(child)

    link_c1 = PageLink(
        from_page_id=c1.id,
        to_page_id=q.id,
        link_type=LinkType.CONSIDERATION,
        direction=ConsiderationDirection.SUPPORTS,
        strength=3.0,
    )
    link_c2 = PageLink(
        from_page_id=c2.id,
        to_page_id=q.id,
        link_type=LinkType.CONSIDERATION,
        direction=ConsiderationDirection.OPPOSES,
        strength=2.0,
    )
    link_child = PageLink(
        from_page_id=q.id,
        to_page_id=child.id,
        link_type=LinkType.CHILD_QUESTION,
    )
    for link in (link_c1, link_c2, link_child):
        await tmp_db.save_link(link)

    return q, c1, c2, child, link_c1, link_c2, link_child


async def test_gather_connected_pages_finds_considerations(
    tmp_db, question_with_considerations,
):
    q, c1, c2, child, *_ = question_with_considerations
    connected = await _gather_connected_pages(q.id, tmp_db)
    page_ids = {p.id for p, _ in connected}
    assert c1.id in page_ids
    assert c2.id in page_ids
    assert child.id in page_ids


async def test_gather_connected_pages_finds_judgements(tmp_db):
    q = _question()
    j = _judgement()
    await tmp_db.save_page(q)
    await tmp_db.save_page(j)
    await tmp_db.save_link(PageLink(
        from_page_id=j.id,
        to_page_id=q.id,
        link_type=LinkType.ANSWERS,
    ))

    connected = await _gather_connected_pages(q.id, tmp_db)
    page_ids = {p.id for p, _ in connected}
    assert j.id in page_ids


async def test_gather_connected_pages_includes_superseded(tmp_db):
    """Superseded pages should be included so Phase A can detect and swap them."""
    q = _question()
    old_claim = _claim("Old")
    new_claim = _claim("New")
    await tmp_db.save_page(q)
    await tmp_db.save_page(old_claim)
    await tmp_db.save_page(new_claim)
    await tmp_db.save_link(PageLink(
        from_page_id=old_claim.id,
        to_page_id=q.id,
        link_type=LinkType.CONSIDERATION,
    ))
    await tmp_db.supersede_page(old_claim.id, new_claim.id)

    connected = await _gather_connected_pages(q.id, tmp_db)
    page_ids = {p.id for p, _ in connected}
    assert old_claim.id in page_ids


async def test_get_latest_judgement_returns_most_recent(tmp_db):
    q = _question()
    j1 = _judgement("First")
    j2 = _judgement("Second")
    await tmp_db.save_page(q)
    await tmp_db.save_page(j1)
    await tmp_db.save_page(j2)
    for j in (j1, j2):
        await tmp_db.save_link(PageLink(
            from_page_id=j.id,
            to_page_id=q.id,
            link_type=LinkType.ANSWERS,
        ))

    latest = await _get_latest_judgement(q.id, tmp_db)
    assert latest is not None
    assert latest.id == j2.id


async def test_get_latest_judgement_returns_none_when_no_judgements(tmp_db):
    q = _question()
    await tmp_db.save_page(q)
    latest = await _get_latest_judgement(q.id, tmp_db)
    assert latest is None


async def test_swap_superseded_link_updates_from_page(tmp_db):
    """When the linked page is the from_page, the new link should update from_page_id."""
    q = _question()
    old = _claim("Old claim")
    new = _claim("New claim")
    await tmp_db.save_page(q)
    await tmp_db.save_page(old)
    await tmp_db.save_page(new)
    link = PageLink(
        from_page_id=old.id,
        to_page_id=q.id,
        link_type=LinkType.CONSIDERATION,
        direction=ConsiderationDirection.SUPPORTS,
        strength=4.0,
        reasoning="important",
    )
    await tmp_db.save_link(link)

    new_link = await _swap_superseded_link(old, link, new, tmp_db)

    assert new_link.from_page_id == new.id
    assert new_link.to_page_id == q.id
    assert new_link.link_type == LinkType.CONSIDERATION
    assert new_link.direction == ConsiderationDirection.SUPPORTS
    assert new_link.strength == 4.0
    assert new_link.reasoning == "important"

    old_link = await tmp_db.get_link(link.id)
    assert old_link is None


async def test_swap_superseded_link_updates_to_page(tmp_db):
    """When the linked page is the to_page, the new link should update to_page_id."""
    parent = _question("Parent")
    old_child = _question("Old child")
    new_child = _question("New child")
    await tmp_db.save_page(parent)
    await tmp_db.save_page(old_child)
    await tmp_db.save_page(new_child)
    link = PageLink(
        from_page_id=parent.id,
        to_page_id=old_child.id,
        link_type=LinkType.CHILD_QUESTION,
    )
    await tmp_db.save_link(link)

    new_link = await _swap_superseded_link(old_child, link, new_child, tmp_db)

    assert new_link.from_page_id == parent.id
    assert new_link.to_page_id == new_child.id


async def test_phase_a_swaps_superseded_pages(tmp_db):
    q = _question()
    old = _claim("Old claim")
    new = _claim("New claim")
    await tmp_db.save_page(q)
    await tmp_db.save_page(old)
    await tmp_db.save_page(new)
    await tmp_db.save_link(PageLink(
        from_page_id=old.id,
        to_page_id=q.id,
        link_type=LinkType.CONSIDERATION,
    ))
    await tmp_db.supersede_page(old.id, new.id)

    connected = await _resolve_superseded_connections(q.id, None, tmp_db)
    page_ids = {p.id for p, _ in connected}
    assert new.id in page_ids
    assert old.id not in page_ids


async def test_phase_a_keeps_active_pages(tmp_db, question_with_considerations):
    q, c1, c2, child, *_ = question_with_considerations
    connected = await _resolve_superseded_connections(q.id, None, tmp_db)
    page_ids = {p.id for p, _ in connected}
    assert c1.id in page_ids
    assert c2.id in page_ids
    assert child.id in page_ids


async def test_phase_a_includes_judgement_connections(tmp_db):
    """Phase A should also gather connected pages of the latest judgement."""
    q = _question()
    j = _judgement()
    claim_on_j = _claim("Claim bearing on judgement")
    await tmp_db.save_page(q)
    await tmp_db.save_page(j)
    await tmp_db.save_page(claim_on_j)
    await tmp_db.save_link(PageLink(
        from_page_id=j.id,
        to_page_id=q.id,
        link_type=LinkType.ANSWERS,
    ))
    await tmp_db.save_link(PageLink(
        from_page_id=claim_on_j.id,
        to_page_id=j.id,
        link_type=LinkType.CONSIDERATION,
    ))

    connected = await _resolve_superseded_connections(q.id, j, tmp_db)
    page_ids = {p.id for p, _ in connected}
    assert j.id in page_ids
    assert claim_on_j.id in page_ids


async def test_cites_superseded_pages_true(tmp_db):
    cited = _claim("Cited claim")
    replacement = _claim("Replacement")
    target = _claim(
        "Target",
        content=f"This depends on [{cited.id[:8]}] for support.",
    )
    await tmp_db.save_page(cited)
    await tmp_db.save_page(replacement)
    await tmp_db.save_page(target)
    await tmp_db.supersede_page(cited.id, replacement.id)

    assert await _cites_superseded_pages(target, tmp_db) is True


async def test_cites_superseded_pages_false(tmp_db):
    cited = _claim("Active cited claim")
    target = _claim(
        "Target",
        content=f"This depends on [{cited.id[:8]}] for support.",
    )
    await tmp_db.save_page(cited)
    await tmp_db.save_page(target)

    assert await _cites_superseded_pages(target, tmp_db) is False


async def test_cites_superseded_pages_no_citations(tmp_db):
    target = _claim("Standalone", content="No citations here.")
    await tmp_db.save_page(target)
    assert await _cites_superseded_pages(target, tmp_db) is False


async def test_phase_a_deduplicates_shared_links(tmp_db):
    """If a page is linked to both the question and its judgement, it should
    appear only once in the connected set."""
    q = _question()
    j = _judgement()
    shared = _claim("Shared claim")
    await tmp_db.save_page(q)
    await tmp_db.save_page(j)
    await tmp_db.save_page(shared)
    await tmp_db.save_link(PageLink(
        from_page_id=j.id,
        to_page_id=q.id,
        link_type=LinkType.ANSWERS,
    ))
    link_to_q = PageLink(
        from_page_id=shared.id,
        to_page_id=q.id,
        link_type=LinkType.CONSIDERATION,
    )
    link_to_j = PageLink(
        from_page_id=shared.id,
        to_page_id=j.id,
        link_type=LinkType.CONSIDERATION,
    )
    await tmp_db.save_link(link_to_q)
    await tmp_db.save_link(link_to_j)

    connected = await _resolve_superseded_connections(q.id, j, tmp_db)
    link_ids = [link.id for _, link in connected]
    assert len(link_ids) == len(set(link_ids)), "Links should be deduplicated"
