"""Tests for fork-at-snapshot staging semantics.

See ``marketplace-thread/11-staging-concurrency.md`` §4 for the spec.

The invariant under test: when a staged DB is created with
``snapshot_ts`` pinned (the default when ``staged=True``), subsequent
baseline writes by *other* runs — new pages, supersessions, etc. — are
invisible to the staged run. The staged run always sees its own writes,
regardless of when they happened.
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


async def _make_db(
    project_id: str,
    staged: bool = False,
) -> DB:
    """Create a DB. Staged DBs pin snapshot_ts to server-now (the default)."""
    db = await DB.create(run_id=str(uuid.uuid4()), staged=staged)
    db.project_id = project_id
    return db


async def _make_page(
    db: DB,
    headline: str,
    page_type: PageType = PageType.CLAIM,
) -> Page:
    page = Page(
        page_type=page_type,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=f"content: {headline}",
        headline=headline,
    )
    await db.save_page(page)
    return page


async def _link(db: DB, src: Page, dst: Page) -> PageLink:
    link = PageLink(
        from_page_id=src.id,
        to_page_id=dst.id,
        link_type=LinkType.CONSIDERATION,
        strength=3.0,
        reasoning="test link",
    )
    await db.save_link(link)
    return link


@pytest_asyncio.fixture
async def project_id():
    setup = await DB.create(run_id=str(uuid.uuid4()))
    project, _ = await setup.get_or_create_project(f"test-snap-{uuid.uuid4().hex[:8]}")
    yield project.id
    await setup._execute(setup.client.table("projects").delete().eq("id", project.id))


@pytest.mark.asyncio
async def test_staged_run_does_not_see_later_baseline_pages(project_id):
    baseline = await _make_db(project_id, staged=False)
    await baseline.init_budget(100)
    pre_snapshot_page = await _make_page(baseline, "pre-snapshot claim")
    # Wait a moment so server-side now() meaningfully advances past the insert.
    await asyncio.sleep(0.05)

    staged = await _make_db(project_id, staged=True)
    assert staged.snapshot_ts is not None

    await asyncio.sleep(0.05)
    post_snapshot_page = await _make_page(baseline, "post-snapshot claim")

    pages = await staged.get_pages()
    ids = {p.id for p in pages}
    assert pre_snapshot_page.id in ids
    assert post_snapshot_page.id not in ids

    single_lookup = await staged.get_page(post_snapshot_page.id)
    assert single_lookup is None

    await baseline.delete_run_data()
    await staged.delete_run_data()


@pytest.mark.asyncio
async def test_staged_run_sees_its_own_pages_created_after_snapshot(project_id):
    baseline = await _make_db(project_id, staged=False)
    await baseline.init_budget(100)
    staged = await _make_db(project_id, staged=True)
    await staged.init_budget(100)
    assert staged.snapshot_ts is not None

    own_page = await _make_page(staged, "own staged claim")

    pages = await staged.get_pages()
    assert own_page.id in {p.id for p in pages}

    fetched = await staged.get_page(own_page.id)
    assert fetched is not None
    assert fetched.id == own_page.id

    await baseline.delete_run_data()
    await staged.delete_run_data()


@pytest.mark.asyncio
async def test_staged_run_does_not_see_later_supersession(project_id):
    baseline = await _make_db(project_id, staged=False)
    await baseline.init_budget(100)
    original = await _make_page(baseline, "original claim")
    await asyncio.sleep(0.05)

    staged = await _make_db(project_id, staged=True)

    await asyncio.sleep(0.05)
    replacement = await _make_page(baseline, "baseline replacement")
    await baseline.supersede_page(original.id, replacement.id)

    page = await staged.get_page(original.id)
    assert page is not None
    assert page.is_superseded is False, (
        "staged run should see the pre-snapshot state of the page, "
        "not the later baseline supersession"
    )
    assert page.superseded_by is None

    await baseline.delete_run_data()
    await staged.delete_run_data()


@pytest.mark.asyncio
async def test_retroactive_stage_respects_original_run_timing(project_id):
    baseline = await _make_db(project_id, staged=False)
    await baseline.init_budget(100)
    run_db = await _make_db(project_id, staged=False)
    await run_db.init_budget(100)
    helper = await _make_db(project_id, staged=False)

    pre_page = await _make_page(baseline, "pre-run baseline")
    q = await _make_page(baseline, "question", PageType.QUESTION)
    baseline_link = await _link(baseline, pre_page, q)

    replacement = await _make_page(run_db, "run replacement")
    await run_db.supersede_page(pre_page.id, replacement.id)
    await run_db.delete_link(baseline_link.id)

    await helper.stage_run(run_db.run_id)

    staged_reader = await DB.create(
        run_id=run_db.run_id,
        staged=True,
        snapshot_ts=None,
    )
    staged_reader.project_id = project_id

    page = await staged_reader.get_page(pre_page.id)
    assert page is not None
    assert page.is_superseded is True
    assert page.superseded_by == replacement.id

    links = await staged_reader.get_links_to(q.id)
    assert all(l.id != baseline_link.id for l in links)

    pages_all = await staged_reader.get_pages()
    assert replacement.id in {p.id for p in pages_all}

    await baseline.delete_run_data()
    await run_db.delete_run_data()
    await helper.delete_run_data()


@pytest.mark.asyncio
async def test_project_isolation_holds_across_staged_runs():
    setup = await DB.create(run_id=str(uuid.uuid4()))
    project_a, _ = await setup.get_or_create_project(f"snap-a-{uuid.uuid4().hex[:6]}")
    project_b, _ = await setup.get_or_create_project(f"snap-b-{uuid.uuid4().hex[:6]}")

    baseline_a = await _make_db(project_a.id, staged=False)
    await baseline_a.init_budget(100)
    baseline_b = await _make_db(project_b.id, staged=False)
    await baseline_b.init_budget(100)

    page_a = await _make_page(baseline_a, "project A page")
    page_b = await _make_page(baseline_b, "project B page")
    await asyncio.sleep(0.05)

    staged_a = await _make_db(project_a.id, staged=True)
    staged_b = await _make_db(project_b.id, staged=True)

    own_a = await _make_page(staged_a, "own A")
    own_b = await _make_page(staged_b, "own B")

    pages_a = await staged_a.get_pages()
    pages_b = await staged_b.get_pages()
    ids_a = {p.id for p in pages_a}
    ids_b = {p.id for p in pages_b}

    assert page_a.id in ids_a
    assert own_a.id in ids_a
    assert page_b.id not in ids_a
    assert own_b.id not in ids_a

    assert page_b.id in ids_b
    assert own_b.id in ids_b
    assert page_a.id not in ids_b
    assert own_a.id not in ids_b

    await baseline_a.delete_run_data()
    await baseline_b.delete_run_data()
    await staged_a.delete_run_data()
    await staged_b.delete_run_data()
    await setup._execute(setup.client.table("projects").delete().eq("id", project_a.id))
    await setup._execute(setup.client.table("projects").delete().eq("id", project_b.id))


@pytest.mark.asyncio
async def test_snapshot_ts_filters_get_pages_by_ids(project_id):
    """Bulk fetch honours the snapshot boundary for baseline pages."""
    baseline = await _make_db(project_id, staged=False)
    await baseline.init_budget(100)
    pre_page = await _make_page(baseline, "pre-snap")
    await asyncio.sleep(0.05)

    staged = await _make_db(project_id, staged=True)
    await asyncio.sleep(0.05)
    post_page = await _make_page(baseline, "post-snap")

    fetched = await staged.get_pages_by_ids([pre_page.id, post_page.id])
    assert pre_page.id in fetched
    assert post_page.id not in fetched

    await baseline.delete_run_data()
    await staged.delete_run_data()


@pytest.mark.asyncio
async def test_snapshot_ts_filters_get_links_to(project_id):
    """Link reads honour the snapshot boundary as well."""
    baseline = await _make_db(project_id, staged=False)
    await baseline.init_budget(100)
    q = await _make_page(baseline, "q", PageType.QUESTION)
    claim = await _make_page(baseline, "claim")
    pre_link = await _link(baseline, claim, q)
    await asyncio.sleep(0.05)

    staged = await _make_db(project_id, staged=True)
    await asyncio.sleep(0.05)
    post_claim = await _make_page(baseline, "later claim")
    post_link = await _link(baseline, post_claim, q)

    links = await staged.get_links_to(q.id)
    ids = {l.id for l in links}
    assert pre_link.id in ids
    assert post_link.id not in ids

    await baseline.delete_run_data()
    await staged.delete_run_data()


@pytest.mark.asyncio
async def test_nonstaged_db_ignores_snapshot(project_id):
    """Non-staged DBs default snapshot_ts=None and see everything current."""
    baseline = await _make_db(project_id, staged=False)
    await baseline.init_budget(100)
    assert baseline.snapshot_ts is None

    page = await _make_page(baseline, "baseline page")
    await asyncio.sleep(0.05)
    later_page = await _make_page(baseline, "later baseline page")

    observer = await _make_db(project_id, staged=False)
    assert observer.snapshot_ts is None
    ids = {p.id for p in await observer.get_pages()}
    assert page.id in ids
    assert later_page.id in ids

    await baseline.delete_run_data()
    await observer.delete_run_data()


@pytest.mark.xfail(
    reason=(
        "Known gap: baseline delete_link after a staged run's snapshot "
        "physically removes the row from page_links (non-staged delete "
        "issues DELETE FROM). A staged reader at an earlier snapshot then "
        "sees the row as gone even though it existed at snapshot time. "
        "TODO: reconstruct the deleted row from the delete_link event "
        "payload in MutationState._load_mutation_state, mirroring the "
        "unapply_supersessions / unapply_update_content pattern at "
        "database.py:~480. Flagged in audit M1 (marketplace-thread/33-wave-audit.md)."
    ),
    strict=True,
)
@pytest.mark.asyncio
async def test_staged_run_still_sees_link_baseline_deleted_after_snapshot(project_id):
    """Fork-at-snapshot should make a baseline link deletion invisible to
    a staged reader whose snapshot pre-dates the delete. Currently fails
    because delete_link on a non-staged run physically removes the row
    and the event-overlay pass doesn't re-insert it. See xfail reason.
    """
    baseline = await _make_db(project_id, staged=False)
    await baseline.init_budget(100)
    src = await _make_page(baseline, "source")
    dst = await _make_page(baseline, "dest")
    link = await _link(baseline, src, dst)
    await asyncio.sleep(0.05)

    staged = await _make_db(project_id, staged=True)
    assert staged.snapshot_ts is not None

    await asyncio.sleep(0.05)
    await baseline.delete_link(link.id)

    links = await staged.get_links_from(src.id)
    try:
        assert link.id in {lk.id for lk in links}, (
            "staged reader at pre-delete snapshot should still see the link"
        )
    finally:
        await baseline.delete_run_data()
        await staged.delete_run_data()


@pytest.mark.asyncio
async def test_fork_preserves_snapshot_ts(project_id):
    baseline = await _make_db(project_id, staged=False)
    await baseline.init_budget(100)
    staged = await _make_db(project_id, staged=True)
    assert staged.snapshot_ts is not None

    child = await staged.fork()
    try:
        assert child.snapshot_ts == staged.snapshot_ts
        assert child.staged is True
        assert child.run_id == staged.run_id
    finally:
        await child.close()

    await baseline.delete_run_data()
    await staged.delete_run_data()
