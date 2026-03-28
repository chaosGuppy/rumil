"""Test staged run isolation: pages, links, superseding, and link mutations."""

import uuid

import pytest_asyncio

from rumil.database import DB
from rumil.models import (
    LinkRole,
    LinkType,
    Page,
    PageLayer,
    PageLink,
    PageType,
    Workspace,
)


async def _make_db(project_id: str, staged: bool = False) -> DB:
    db = await DB.create(run_id=str(uuid.uuid4()), staged=staged)
    db.project_id = project_id
    return db


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


async def _link(db: DB, from_page: Page, to_page: Page) -> PageLink:
    link = PageLink(
        from_page_id=from_page.id,
        to_page_id=to_page.id,
        link_type=LinkType.CONSIDERATION,
        strength=5.0,
        reasoning="test link",
    )
    await db.save_link(link)
    return link


@pytest_asyncio.fixture
async def project_id():
    db = await DB.create(run_id=str(uuid.uuid4()))
    project = await db.get_or_create_project(f"test-staged-{uuid.uuid4().hex[:8]}")
    return project.id


@pytest_asyncio.fixture
async def baseline_db(project_id):
    """A non-staged DB that creates baseline data."""
    db = await _make_db(project_id, staged=False)
    await db.init_budget(100)
    yield db
    await db.delete_run_data()


@pytest_asyncio.fixture
async def staged_db(project_id):
    """A staged DB whose writes should be isolated."""
    db = await _make_db(project_id, staged=True)
    await db.init_budget(100)
    yield db
    await db.delete_run_data()


@pytest_asyncio.fixture
async def observer_db(project_id):
    """A non-staged DB that observes — should never see staged data."""
    db = await _make_db(project_id, staged=False)
    yield db
    await db.delete_run_data()


async def test_staged_pages_invisible_to_observers(baseline_db, staged_db, observer_db):
    """Pages created by a staged run are visible to that run but not to others."""
    baseline_page = await _make_page(baseline_db, "baseline claim")
    staged_page = await _make_page(staged_db, "staged claim")

    # Staged run sees both
    staged_pages = await staged_db.get_pages()
    staged_ids = {p.id for p in staged_pages}
    assert baseline_page.id in staged_ids
    assert staged_page.id in staged_ids

    # Observer sees only baseline
    observer_pages = await observer_db.get_pages()
    observer_ids = {p.id for p in observer_pages}
    assert baseline_page.id in observer_ids
    assert staged_page.id not in observer_ids


async def test_staged_links_invisible_to_observers(baseline_db, staged_db, observer_db):
    """Links created by a staged run are visible to that run but not to others."""
    q = await _make_page(baseline_db, "question", PageType.QUESTION)
    baseline_claim = await _make_page(baseline_db, "baseline claim")
    await _link(baseline_db, baseline_claim, q)

    staged_claim = await _make_page(staged_db, "staged claim")
    await _link(staged_db, staged_claim, q)

    # Staged run sees both links
    staged_links = await staged_db.get_links_to(q.id)
    staged_from_ids = {l.from_page_id for l in staged_links}
    assert baseline_claim.id in staged_from_ids
    assert staged_claim.id in staged_from_ids

    # Observer sees only baseline link
    observer_links = await observer_db.get_links_to(q.id)
    observer_from_ids = {l.from_page_id for l in observer_links}
    assert baseline_claim.id in observer_from_ids
    assert staged_claim.id not in observer_from_ids


async def test_staged_supersede_is_isolated(baseline_db, staged_db, observer_db):
    """A staged run superseding a baseline page only affects that staged run's view."""
    old_page = await _make_page(baseline_db, "original claim")
    new_page = await _make_page(staged_db, "replacement claim")

    await staged_db.supersede_page(old_page.id, new_page.id)

    # Staged run sees old page as superseded
    staged_pages = await staged_db.get_pages(active_only=True)
    staged_ids = {p.id for p in staged_pages}
    assert old_page.id not in staged_ids
    assert new_page.id in staged_ids

    # Observer still sees the old page as active
    observer_pages = await observer_db.get_pages(active_only=True)
    observer_ids = {p.id for p in observer_pages}
    assert old_page.id in observer_ids


async def test_staged_delete_link_is_isolated(baseline_db, staged_db, observer_db):
    """A staged run deleting a baseline link only affects that staged run's view."""
    q = await _make_page(baseline_db, "question", PageType.QUESTION)
    claim = await _make_page(baseline_db, "some claim")
    link = await _link(baseline_db, claim, q)

    await staged_db.delete_link(link.id)

    # Staged run no longer sees the link
    staged_links = await staged_db.get_links_to(q.id)
    assert all(l.id != link.id for l in staged_links)

    # Observer still sees it
    observer_links = await observer_db.get_links_to(q.id)
    assert any(l.id == link.id for l in observer_links)


async def test_staged_change_link_role_is_isolated(baseline_db, staged_db, observer_db):
    """A staged run changing a link role only affects that staged run's view."""
    q = await _make_page(baseline_db, "question", PageType.QUESTION)
    claim = await _make_page(baseline_db, "some claim")
    link = await _link(baseline_db, claim, q)
    assert link.role == LinkRole.DIRECT

    await staged_db.update_link_role(link.id, LinkRole.STRUCTURAL)

    # Staged run sees the new role
    staged_links = await staged_db.get_links_to(q.id)
    staged_link = next(l for l in staged_links if l.id == link.id)
    assert staged_link.role == LinkRole.STRUCTURAL

    # Observer sees the original role
    observer_links = await observer_db.get_links_to(q.id)
    observer_link = next(l for l in observer_links if l.id == link.id)
    assert observer_link.role == LinkRole.DIRECT


async def test_two_staged_runs_are_isolated_from_each_other(project_id):
    """Two different staged runs cannot see each other's data."""
    db_a = await _make_db(project_id, staged=True)
    await db_a.init_budget(100)
    db_b = await _make_db(project_id, staged=True)
    await db_b.init_budget(100)

    page_a = await _make_page(db_a, "arm A claim")
    page_b = await _make_page(db_b, "arm B claim")

    pages_a = await db_a.get_pages()
    pages_b = await db_b.get_pages()

    assert page_a.id in {p.id for p in pages_a}
    assert page_b.id not in {p.id for p in pages_a}

    assert page_b.id in {p.id for p in pages_b}
    assert page_a.id not in {p.id for p in pages_b}

    await db_a.delete_run_data()
    await db_b.delete_run_data()


async def test_get_pages_by_ids_respects_staged_events(baseline_db, staged_db, observer_db):
    """get_pages_by_ids applies supersede events from the staged run."""
    page = await _make_page(baseline_db, "will be superseded")
    replacement = await _make_page(staged_db, "replacement")
    await staged_db.supersede_page(page.id, replacement.id)

    staged_result = await staged_db.get_pages_by_ids([page.id])
    assert page.id in staged_result
    assert staged_result[page.id].is_superseded is True

    observer_result = await observer_db.get_pages_by_ids([page.id])
    assert page.id in observer_result
    assert observer_result[page.id].is_superseded is False
