"""Tests for save_link dedup: duplicate (from, to, link_type) rows are skipped."""

import uuid
from datetime import UTC, datetime

import pytest_asyncio

from rumil.database import DB
from rumil.models import (
    LinkType,
    Page,
    PageLayer,
    PageLink,
    PageType,
    Workspace,
)
from rumil.settings import override_settings


async def _make_page(db: DB, headline: str, page_type: PageType = PageType.CLAIM) -> Page:
    page = Page(
        page_type=page_type,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=f"Content for: {headline}",
        headline=headline,
    )
    await db.save_page(page)
    return page


async def _save_cites(
    db: DB,
    from_page: Page,
    to_page: Page,
    *,
    link_type: LinkType = LinkType.CITES,
    reasoning: str = "test",
    strength: float = 0.5,
) -> PageLink:
    link = PageLink(
        from_page_id=from_page.id,
        to_page_id=to_page.id,
        link_type=link_type,
        strength=strength,
        reasoning=reasoning,
    )
    await db.save_link(link)
    return link


async def _count_links(db: DB, from_id: str, to_id: str, link_type: LinkType) -> int:
    query = (
        db.client.table("page_links")
        .select("id")
        .eq("from_page_id", from_id)
        .eq("to_page_id", to_id)
        .eq("link_type", link_type.value)
    )
    response = await db._execute(query)
    return len(response.data) if response.data else 0


@pytest_asyncio.fixture
async def project_id():
    db = await DB.create(run_id=str(uuid.uuid4()))
    project, _ = await db.get_or_create_project(f"test-dedup-{uuid.uuid4().hex[:8]}")
    yield project.id
    await db._execute(db.client.table("projects").delete().eq("id", project.id))


@pytest_asyncio.fixture
async def baseline_db(project_id):
    db = await DB.create(run_id=str(uuid.uuid4()), staged=False)
    db.project_id = project_id
    await db.init_budget(100)
    yield db
    await db.delete_run_data()


@pytest_asyncio.fixture
async def staged_db(project_id):
    """Staged DB with loose-staging (snapshot pinned to far future) so it sees
    baseline data seeded after the fixture is created."""
    db = await DB.create(
        run_id=str(uuid.uuid4()),
        staged=True,
        snapshot_ts=datetime.max.replace(tzinfo=UTC),
    )
    db.project_id = project_id
    await db.init_budget(100)
    yield db
    await db.delete_run_data()


async def test_duplicate_cites_link_skipped(baseline_db):
    a = await _make_page(baseline_db, "claim A")
    b = await _make_page(baseline_db, "source B", PageType.SOURCE)

    await _save_cites(baseline_db, a, b)
    await _save_cites(baseline_db, a, b)

    assert await _count_links(baseline_db, a.id, b.id, LinkType.CITES) == 1


async def test_reverse_direction_is_not_duplicate(baseline_db):
    a = await _make_page(baseline_db, "claim A")
    b = await _make_page(baseline_db, "claim B")

    await _save_cites(baseline_db, a, b)
    await _save_cites(baseline_db, b, a)

    assert await _count_links(baseline_db, a.id, b.id, LinkType.CITES) == 1
    assert await _count_links(baseline_db, b.id, a.id, LinkType.CITES) == 1


async def test_different_link_type_is_not_duplicate(baseline_db):
    a = await _make_page(baseline_db, "claim A")
    b = await _make_page(baseline_db, "claim B")

    await _save_cites(baseline_db, a, b, link_type=LinkType.CITES)
    await _save_cites(baseline_db, a, b, link_type=LinkType.DEPENDS_ON)

    assert await _count_links(baseline_db, a.id, b.id, LinkType.CITES) == 1
    assert await _count_links(baseline_db, a.id, b.id, LinkType.DEPENDS_ON) == 1


async def test_dedup_disabled_allows_duplicates(baseline_db):
    a = await _make_page(baseline_db, "claim A")
    b = await _make_page(baseline_db, "source B", PageType.SOURCE)

    with override_settings(dedupe_page_links=False):
        link1 = PageLink(
            from_page_id=a.id,
            to_page_id=b.id,
            link_type=LinkType.CITES,
            reasoning="first",
        )
        link2 = PageLink(
            from_page_id=a.id,
            to_page_id=b.id,
            link_type=LinkType.CITES,
            reasoning="second",
        )
        await baseline_db.save_link(link1)
        await baseline_db.save_link(link2)

    assert await _count_links(baseline_db, a.id, b.id, LinkType.CITES) == 2


async def test_staged_dedup_isolated_from_baseline(baseline_db, staged_db):
    a = await _make_page(baseline_db, "claim A")
    b = await _make_page(baseline_db, "source B", PageType.SOURCE)

    # Baseline writes the link first.
    await _save_cites(baseline_db, a, b)

    # Staged run tries to add "the same" link. Since staged sees baseline,
    # it should dedup and skip. Staged should still see exactly one link.
    await _save_cites(staged_db, a, b)

    staged_links = await staged_db.get_links_to(b.id)
    staged_cites = [l for l in staged_links if l.link_type == LinkType.CITES]
    assert len(staged_cites) == 1

    # Baseline also sees exactly one (its own).
    baseline_links = await baseline_db.get_links_to(b.id)
    baseline_cites = [l for l in baseline_links if l.link_type == LinkType.CITES]
    assert len(baseline_cites) == 1


async def test_first_writer_wins_on_reasoning_strength(baseline_db):
    a = await _make_page(baseline_db, "claim A")
    b = await _make_page(baseline_db, "source B", PageType.SOURCE)

    first = await _save_cites(baseline_db, a, b, reasoning="original reasoning", strength=0.3)
    await _save_cites(baseline_db, a, b, reasoning="different reasoning", strength=0.9)

    assert await _count_links(baseline_db, a.id, b.id, LinkType.CITES) == 1

    stored = await baseline_db.get_link(first.id)
    assert stored is not None
    assert stored.reasoning == "original reasoning"
    assert stored.strength == 0.3
