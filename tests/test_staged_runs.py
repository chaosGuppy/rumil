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


async def test_nonstaged_runs_record_mutation_events(baseline_db, observer_db):
    """Non-staged mutations now record events in mutation_events table."""
    q = await _make_page(baseline_db, "question", PageType.QUESTION)
    claim = await _make_page(baseline_db, "original claim")
    link = await _link(baseline_db, claim, q)

    replacement = await _make_page(observer_db, "replacement claim")
    await observer_db.supersede_page(claim.id, replacement.id)
    await observer_db.delete_link(link.id)

    q2 = await _make_page(baseline_db, "question 2", PageType.QUESTION)
    claim2 = await _make_page(baseline_db, "claim 2")
    link2 = await _link(baseline_db, claim2, q2)
    await observer_db.update_link_role(link2.id, LinkRole.STRUCTURAL)

    events = (
        await observer_db._execute(
            observer_db.client.table("mutation_events")
            .select("event_type, target_id")
            .eq("run_id", observer_db.run_id)
            .order("created_at")
        )
    ).data
    event_types = [e["event_type"] for e in events]
    target_ids = [e["target_id"] for e in events]

    assert "supersede_page" in event_types
    assert "delete_link" in event_types
    assert "change_link_role" in event_types
    assert claim.id in target_ids
    assert link.id in target_ids
    assert link2.id in target_ids


async def test_stage_run_hides_pages_from_baseline(project_id):
    """After stage_run(), the run's pages and links are invisible to baseline readers."""
    run_db = await _make_db(project_id, staged=False)
    await run_db.init_budget(100)
    observer = await _make_db(project_id, staged=False)

    q = await _make_page(run_db, "question", PageType.QUESTION)
    claim = await _make_page(run_db, "a claim")
    await _link(run_db, claim, q)

    # Before staging: observer sees everything
    obs_pages = await observer.get_pages()
    assert q.id in {p.id for p in obs_pages}
    assert claim.id in {p.id for p in obs_pages}

    await observer.stage_run(run_db.run_id)

    # After staging: observer sees nothing from that run
    obs_pages = await observer.get_pages()
    obs_ids = {p.id for p in obs_pages}
    assert q.id not in obs_ids
    assert claim.id not in obs_ids

    obs_links = await observer.get_links_to(q.id)
    assert len(obs_links) == 0

    await run_db.delete_run_data()
    await observer.delete_run_data()


async def test_stage_run_reverts_supersession(project_id):
    """stage_run() restores a baseline page that the run had superseded."""
    baseline = await _make_db(project_id, staged=False)
    await baseline.init_budget(100)
    run_db = await _make_db(project_id, staged=False)
    await run_db.init_budget(100)
    observer = await _make_db(project_id, staged=False)

    original = await _make_page(baseline, "original claim")
    replacement = await _make_page(run_db, "better claim")
    await run_db.supersede_page(original.id, replacement.id)

    # Before staging: observer sees original as superseded
    obs_page = await observer.get_page(original.id)
    assert obs_page is not None
    assert obs_page.is_superseded is True

    await observer.stage_run(run_db.run_id)

    # After staging: observer sees original as active again
    obs_page = await observer.get_page(original.id)
    assert obs_page is not None
    assert obs_page.is_superseded is False
    assert obs_page.superseded_by is None

    await baseline.delete_run_data()
    await run_db.delete_run_data()
    await observer.delete_run_data()


async def test_stage_run_restores_deleted_link(project_id):
    """stage_run() re-inserts a baseline link that the run had deleted."""
    baseline = await _make_db(project_id, staged=False)
    await baseline.init_budget(100)
    run_db = await _make_db(project_id, staged=False)
    await run_db.init_budget(100)
    observer = await _make_db(project_id, staged=False)

    q = await _make_page(baseline, "question", PageType.QUESTION)
    claim = await _make_page(baseline, "some claim")
    link = await _link(baseline, claim, q)

    await run_db.delete_link(link.id)

    # Before staging: link is gone
    obs_links = await observer.get_links_to(q.id)
    assert all(l.id != link.id for l in obs_links)

    await observer.stage_run(run_db.run_id)

    # After staging: link is restored
    obs_links = await observer.get_links_to(q.id)
    assert any(l.id == link.id for l in obs_links)

    await baseline.delete_run_data()
    await run_db.delete_run_data()
    await observer.delete_run_data()


async def test_stage_run_reverts_role_change(project_id):
    """stage_run() restores a baseline link's original role."""
    baseline = await _make_db(project_id, staged=False)
    await baseline.init_budget(100)
    run_db = await _make_db(project_id, staged=False)
    await run_db.init_budget(100)
    observer = await _make_db(project_id, staged=False)

    q = await _make_page(baseline, "question", PageType.QUESTION)
    claim = await _make_page(baseline, "some claim")
    link = await _link(baseline, claim, q)
    assert link.role == LinkRole.DIRECT

    await run_db.update_link_role(link.id, LinkRole.STRUCTURAL)

    # Before staging: observer sees STRUCTURAL
    obs_link = await observer.get_link(link.id)
    assert obs_link is not None
    assert obs_link.role == LinkRole.STRUCTURAL

    await observer.stage_run(run_db.run_id)

    # After staging: observer sees original DIRECT
    obs_link = await observer.get_link(link.id)
    assert obs_link is not None
    assert obs_link.role == LinkRole.DIRECT

    await baseline.delete_run_data()
    await run_db.delete_run_data()
    await observer.delete_run_data()


async def test_retroactively_staged_run_indistinguishable(project_id):
    """A retroactively staged run looks identical to a natively staged run."""
    baseline = await _make_db(project_id, staged=False)
    await baseline.init_budget(100)
    run_db = await _make_db(project_id, staged=False)
    await run_db.init_budget(100)
    helper = await _make_db(project_id, staged=False)

    # Baseline state
    original_claim = await _make_page(baseline, "original claim")
    q = await _make_page(baseline, "question", PageType.QUESTION)
    baseline_link = await _link(baseline, original_claim, q)

    # Run creates a page, supersedes the original, and deletes the baseline link
    replacement = await _make_page(run_db, "replacement claim")
    await run_db.supersede_page(original_claim.id, replacement.id)
    await run_db.delete_link(baseline_link.id)

    # Retroactively stage
    await helper.stage_run(run_db.run_id)

    # Open a staged reader for the now-staged run
    staged_reader = await DB.create(run_id=run_db.run_id, staged=True)
    staged_reader.project_id = project_id

    # Staged reader sees the replacement page
    pages = await staged_reader.get_pages()
    page_ids = {p.id for p in pages}
    assert replacement.id in page_ids

    # Staged reader sees original_claim as superseded
    page = await staged_reader.get_page(original_claim.id)
    assert page is not None
    assert page.is_superseded is True
    assert page.superseded_by == replacement.id

    # Staged reader does not see the deleted baseline link
    links = await staged_reader.get_links_to(q.id)
    assert all(l.id != baseline_link.id for l in links)

    # Baseline observer sees none of the run's effects
    observer = await _make_db(project_id, staged=False)
    obs_pages = await observer.get_pages(active_only=True)
    obs_ids = {p.id for p in obs_pages}
    assert original_claim.id in obs_ids
    assert replacement.id not in obs_ids

    obs_links = await observer.get_links_to(q.id)
    assert any(l.id == baseline_link.id for l in obs_links)

    await baseline.delete_run_data()
    await run_db.delete_run_data()
    await helper.delete_run_data()
    await observer.delete_run_data()
