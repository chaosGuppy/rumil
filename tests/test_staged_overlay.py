"""Integration tests for StagedOverlay.

Exercises the three methods ported to ``self.overlay`` (``get_page``,
``get_pages_by_ids``, ``get_links_from``) to confirm the overlay pairs
``_staged_filter`` + ``_apply_*_events`` correctly for staged and baseline
runs, including the retroactive-staging rollback path.
"""

import asyncio
import uuid

import pytest
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


async def _make_db(project_id: str, staged: bool = False) -> DB:
    db = await DB.create(run_id=str(uuid.uuid4()), staged=staged)
    db.project_id = project_id
    db.snapshot_ts = None
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
    project = await db.get_or_create_project(f"test-overlay-{uuid.uuid4().hex[:8]}")
    yield project.id
    await db._execute(db.client.table("projects").delete().eq("id", project.id))


@pytest_asyncio.fixture
async def baseline_db(project_id):
    db = await _make_db(project_id, staged=False)
    await db.init_budget(100)
    yield db
    await db.delete_run_data()


@pytest_asyncio.fixture
async def staged_db(project_id):
    db = await _make_db(project_id, staged=True)
    await db.init_budget(100)
    yield db
    await db.delete_run_data()


@pytest_asyncio.fixture
async def observer_db(project_id):
    db = await _make_db(project_id, staged=False)
    yield db
    await db.delete_run_data()


async def test_overlay_is_attached_to_db(baseline_db):
    assert baseline_db.overlay is not None
    assert baseline_db.overlay._db is baseline_db


async def test_get_page_applies_filter_for_baseline(baseline_db, staged_db, observer_db):
    staged_page = await _make_page(staged_db, "staged claim")

    assert await observer_db.get_page(staged_page.id) is None
    fetched = await staged_db.get_page(staged_page.id)
    assert fetched is not None
    assert fetched.headline == "staged claim"


async def test_get_page_applies_supersede_event(baseline_db, staged_db, observer_db):
    original = await _make_page(baseline_db, "original claim")
    replacement = await _make_page(staged_db, "replacement claim")
    await staged_db.supersede_page(original.id, replacement.id)

    staged_view = await staged_db.get_page(original.id)
    assert staged_view is not None
    assert staged_view.is_superseded is True
    assert staged_view.superseded_by == replacement.id

    observer_view = await observer_db.get_page(original.id)
    assert observer_view is not None
    assert observer_view.is_superseded is False


async def test_get_page_applies_content_override(baseline_db, staged_db, observer_db):
    page = await _make_page(baseline_db, "mutable claim")
    await staged_db.update_page_content(page.id, "staged edit")

    staged_view = await staged_db.get_page(page.id)
    assert staged_view is not None
    assert staged_view.content == "staged edit"

    observer_view = await observer_db.get_page(page.id)
    assert observer_view is not None
    assert observer_view.content == page.content


async def test_get_pages_by_ids_applies_overlay(baseline_db, staged_db, observer_db):
    a = await _make_page(baseline_db, "a")
    b = await _make_page(baseline_db, "b")
    replacement = await _make_page(staged_db, "replacement for a")
    await staged_db.supersede_page(a.id, replacement.id)

    staged_result = await staged_db.get_pages_by_ids([a.id, b.id])
    assert staged_result[a.id].is_superseded is True
    assert staged_result[b.id].is_superseded is False

    observer_result = await observer_db.get_pages_by_ids([a.id, b.id])
    assert observer_result[a.id].is_superseded is False


async def test_get_pages_by_ids_hides_other_run_staged_pages(baseline_db, staged_db, observer_db):
    staged_page = await _make_page(staged_db, "staged only")
    baseline_page = await _make_page(baseline_db, "baseline only")

    result = await observer_db.get_pages_by_ids([staged_page.id, baseline_page.id])
    assert staged_page.id not in result
    assert baseline_page.id in result


async def test_get_links_from_applies_delete_event(baseline_db, staged_db, observer_db):
    q = await _make_page(baseline_db, "q", PageType.QUESTION)
    claim = await _make_page(baseline_db, "claim")
    link = await _link(baseline_db, claim, q)

    await staged_db.delete_link(link.id)

    staged_links = await staged_db.get_links_from(claim.id)
    assert all(l.id != link.id for l in staged_links)

    observer_links = await observer_db.get_links_from(claim.id)
    assert any(l.id == link.id for l in observer_links)


async def test_get_links_from_hides_other_run_staged_links(baseline_db, staged_db, observer_db):
    q = await _make_page(baseline_db, "q", PageType.QUESTION)
    claim = await _make_page(baseline_db, "claim")
    await _link(staged_db, claim, q)

    observer_links = await observer_db.get_links_from(claim.id)
    assert observer_links == []

    staged_links = await staged_db.get_links_from(claim.id)
    assert len(staged_links) == 1


async def test_retroactive_staging_rolls_back_supersession_via_overlay(project_id):
    """After stage_run(), a previously-superseded baseline page reverts for observers.

    Exercises the combination of filter (which hides the staged run's pages/links
    once it is flipped to staged) and event replay (which rolls back direct
    mutations). Routes all reads through the overlay-backed ``get_page``.
    """
    baseline = await _make_db(project_id, staged=False)
    await baseline.init_budget(100)
    run_db = await _make_db(project_id, staged=False)
    await run_db.init_budget(100)
    observer = await _make_db(project_id, staged=False)

    original = await _make_page(baseline, "original")
    replacement = await _make_page(run_db, "better")
    await run_db.supersede_page(original.id, replacement.id)

    pre = await observer.get_page(original.id)
    assert pre is not None
    assert pre.is_superseded is True

    await observer.stage_run(run_db.run_id)

    post = await observer.get_page(original.id)
    assert post is not None
    assert post.is_superseded is False

    # The replacement is now invisible to the observer.
    assert await observer.get_page(replacement.id) is None

    await run_db.delete_run_data()
    await baseline.delete_run_data()
    await observer.delete_run_data()


async def test_overlay_respects_snapshot_ts(project_id):
    baseline = await _make_db(project_id, staged=False)
    await baseline.init_budget(100)
    pre_snapshot_page = await _make_page(baseline, "pre-snapshot")

    staged = await DB.create(run_id=str(uuid.uuid4()), staged=True)
    staged.project_id = project_id
    await staged.init_budget(100)

    assert staged.snapshot_ts is not None
    await asyncio.sleep(1.1)

    post_snapshot_page = await _make_page(baseline, "post-snapshot")

    assert await staged.get_page(pre_snapshot_page.id) is not None
    assert await staged.get_page(post_snapshot_page.id) is None

    await staged.delete_run_data()
    await baseline.delete_run_data()
