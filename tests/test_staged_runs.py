"""Test staged run isolation: pages, links, superseding, and link mutations."""

import uuid

import pytest
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
    yield project.id
    await db._execute(db.client.table("projects").delete().eq("id", project.id))


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


async def _register_run(db: DB) -> None:
    """Register a run in the runs table so commit_staged_run can find it."""
    await db.create_run(name="test", question_id=None)


async def test_commit_staged_run_reveals_pages(project_id):
    """After commit_staged_run(), a natively staged run's pages and links become visible."""
    staged_db = await _make_db(project_id, staged=True)
    await staged_db.init_budget(100)
    await _register_run(staged_db)
    helper = await _make_db(project_id, staged=False)

    q = await _make_page(staged_db, "question", PageType.QUESTION)
    claim = await _make_page(staged_db, "a claim")
    await _link(staged_db, claim, q)

    # Before commit: observer cannot see staged pages/links
    observer = await _make_db(project_id, staged=False)
    obs_pages = await observer.get_pages()
    assert q.id not in {p.id for p in obs_pages}

    await helper.commit_staged_run(staged_db.run_id)

    # After commit: observer sees the pages and links
    obs_pages = await observer.get_pages()
    obs_ids = {p.id for p in obs_pages}
    assert q.id in obs_ids
    assert claim.id in obs_ids

    obs_links = await observer.get_links_to(q.id)
    assert any(l.from_page_id == claim.id for l in obs_links)

    await staged_db.delete_run_data()
    await helper.delete_run_data()
    await observer.delete_run_data()


async def test_commit_staged_run_applies_supersession(project_id):
    """After commit_staged_run(), a staged supersession is applied to the baseline."""
    baseline = await _make_db(project_id, staged=False)
    await baseline.init_budget(100)
    staged_db = await _make_db(project_id, staged=True)
    await staged_db.init_budget(100)
    await _register_run(staged_db)
    helper = await _make_db(project_id, staged=False)

    original = await _make_page(baseline, "original claim")
    replacement = await _make_page(staged_db, "better claim")
    await staged_db.supersede_page(original.id, replacement.id)

    # Before commit: observer sees original as active
    observer = await _make_db(project_id, staged=False)
    obs_page = await observer.get_page(original.id)
    assert obs_page is not None
    assert obs_page.is_superseded is False

    await helper.commit_staged_run(staged_db.run_id)

    # After commit: observer sees original as superseded
    obs_page = await observer.get_page(original.id)
    assert obs_page is not None
    assert obs_page.is_superseded is True
    assert obs_page.superseded_by == replacement.id

    await baseline.delete_run_data()
    await staged_db.delete_run_data()
    await helper.delete_run_data()
    await observer.delete_run_data()


async def test_commit_staged_run_applies_link_deletion(project_id):
    """After commit_staged_run(), a staged link deletion is applied to the baseline."""
    baseline = await _make_db(project_id, staged=False)
    await baseline.init_budget(100)
    staged_db = await _make_db(project_id, staged=True)
    await staged_db.init_budget(100)
    await _register_run(staged_db)
    helper = await _make_db(project_id, staged=False)

    q = await _make_page(baseline, "question", PageType.QUESTION)
    claim = await _make_page(baseline, "some claim")
    link = await _link(baseline, claim, q)

    await staged_db.delete_link(link.id)

    # Before commit: observer still sees the link
    observer = await _make_db(project_id, staged=False)
    obs_links = await observer.get_links_to(q.id)
    assert any(l.id == link.id for l in obs_links)

    await helper.commit_staged_run(staged_db.run_id)

    # After commit: observer no longer sees the link
    obs_links = await observer.get_links_to(q.id)
    assert all(l.id != link.id for l in obs_links)

    await baseline.delete_run_data()
    await staged_db.delete_run_data()
    await helper.delete_run_data()
    await observer.delete_run_data()


async def test_commit_staged_run_applies_role_change(project_id):
    """After commit_staged_run(), a staged role change is applied to the baseline."""
    baseline = await _make_db(project_id, staged=False)
    await baseline.init_budget(100)
    staged_db = await _make_db(project_id, staged=True)
    await staged_db.init_budget(100)
    await _register_run(staged_db)
    helper = await _make_db(project_id, staged=False)

    q = await _make_page(baseline, "question", PageType.QUESTION)
    claim = await _make_page(baseline, "some claim")
    link = await _link(baseline, claim, q)
    assert link.role == LinkRole.DIRECT

    await staged_db.update_link_role(link.id, LinkRole.STRUCTURAL)

    # Before commit: observer sees original role
    observer = await _make_db(project_id, staged=False)
    obs_link = await observer.get_link(link.id)
    assert obs_link is not None
    assert obs_link.role == LinkRole.DIRECT

    await helper.commit_staged_run(staged_db.run_id)

    # After commit: observer sees the new role
    obs_link = await observer.get_link(link.id)
    assert obs_link is not None
    assert obs_link.role == LinkRole.STRUCTURAL

    await baseline.delete_run_data()
    await staged_db.delete_run_data()
    await helper.delete_run_data()
    await observer.delete_run_data()


async def test_commit_staged_run_roundtrip(project_id):
    """stage_run() then commit_staged_run() restores the original non-staged state."""
    baseline = await _make_db(project_id, staged=False)
    await baseline.init_budget(100)
    run_db = await _make_db(project_id, staged=False)
    await run_db.init_budget(100)
    await _register_run(run_db)
    helper = await _make_db(project_id, staged=False)

    original = await _make_page(baseline, "original claim")
    q = await _make_page(baseline, "question", PageType.QUESTION)
    baseline_link = await _link(baseline, original, q)

    replacement = await _make_page(run_db, "replacement claim")
    await run_db.supersede_page(original.id, replacement.id)
    await run_db.delete_link(baseline_link.id)

    # Snapshot the post-run state before staging
    observer = await _make_db(project_id, staged=False)
    pre_stage_page = await observer.get_page(original.id)
    assert pre_stage_page is not None
    assert pre_stage_page.is_superseded is True
    pre_stage_links = await observer.get_links_to(q.id)
    assert all(l.id != baseline_link.id for l in pre_stage_links)

    # Stage, then commit
    await helper.stage_run(run_db.run_id)
    await helper.commit_staged_run(run_db.run_id)

    # After roundtrip: state matches the original non-staged post-run state
    post_page = await observer.get_page(original.id)
    assert post_page is not None
    assert post_page.is_superseded is True
    assert post_page.superseded_by == replacement.id

    post_pages = await observer.get_pages()
    assert replacement.id in {p.id for p in post_pages}

    post_links = await observer.get_links_to(q.id)
    assert all(l.id != baseline_link.id for l in post_links)

    await baseline.delete_run_data()
    await run_db.delete_run_data()
    await helper.delete_run_data()
    await observer.delete_run_data()


async def test_commit_staged_run_validation(project_id):
    """commit_staged_run() raises ValueError for missing or non-staged runs."""
    helper = await _make_db(project_id, staged=False)

    # Non-existent run
    with pytest.raises(ValueError, match="not found"):
        await helper.commit_staged_run("nonexistent-run-id")

    # Non-staged run
    run_db = await _make_db(project_id, staged=False)
    await run_db.init_budget(100)
    await _register_run(run_db)
    await _make_page(run_db, "a page")

    with pytest.raises(ValueError, match="is not staged"):
        await helper.commit_staged_run(run_db.run_id)

    await helper.delete_run_data()
    await run_db.delete_run_data()


async def test_update_page_content_accepted_by_constraint(baseline_db, observer_db):
    """update_page_content() inserts a mutation event and applies the direct page update."""
    page = await _make_page(baseline_db, "headline")
    original_content = page.content
    new_content = "rewritten content body"

    await baseline_db.update_page_content(page.id, new_content=new_content)

    events = (
        await baseline_db._execute(
            baseline_db.client.table("mutation_events")
            .select("event_type, target_id, payload")
            .eq("run_id", baseline_db.run_id)
            .eq("target_id", page.id)
        )
    ).data
    assert len(events) == 1
    assert events[0]["event_type"] == "update_page_content"
    assert events[0]["payload"]["old_content"] == original_content
    assert events[0]["payload"]["new_content"] == new_content

    obs_page = await observer_db.get_page(page.id)
    assert obs_page is not None
    assert obs_page.content == new_content


async def test_stage_run_reverts_update_page_content(project_id):
    """stage_run() restores a baseline page's original content after a non-staged update."""
    baseline = await _make_db(project_id, staged=False)
    await baseline.init_budget(100)
    run_db = await _make_db(project_id, staged=False)
    await run_db.init_budget(100)
    observer = await _make_db(project_id, staged=False)

    page = await _make_page(baseline, "headline")
    original_content = page.content
    new_content = "run-written content"

    await run_db.update_page_content(page.id, new_content=new_content)

    # Before staging: observer sees the updated content
    obs_page = await observer.get_page(page.id)
    assert obs_page is not None
    assert obs_page.content == new_content

    await observer.stage_run(run_db.run_id)

    # After staging: observer sees original content again
    obs_page = await observer.get_page(page.id)
    assert obs_page is not None
    assert obs_page.content == original_content

    await baseline.delete_run_data()
    await run_db.delete_run_data()
    await observer.delete_run_data()


async def test_commit_staged_run_applies_update_page_content(project_id):
    """commit_staged_run() propagates a staged content update to baseline readers."""
    baseline = await _make_db(project_id, staged=False)
    await baseline.init_budget(100)
    staged_db = await _make_db(project_id, staged=True)
    await staged_db.init_budget(100)
    await _register_run(staged_db)
    helper = await _make_db(project_id, staged=False)

    page = await _make_page(baseline, "headline")
    original_content = page.content
    new_content = "committed content"

    await staged_db.update_page_content(page.id, new_content=new_content)

    # Before commit: observer still sees the original content
    observer = await _make_db(project_id, staged=False)
    obs_page = await observer.get_page(page.id)
    assert obs_page is not None
    assert obs_page.content == original_content

    await helper.commit_staged_run(staged_db.run_id)

    # After commit: observer sees the staged content
    obs_page = await observer.get_page(page.id)
    assert obs_page is not None
    assert obs_page.content == new_content

    await baseline.delete_run_data()
    await staged_db.delete_run_data()
    await helper.delete_run_data()
    await observer.delete_run_data()


async def test_staged_update_page_content_is_isolated(baseline_db, staged_db, observer_db):
    """A staged run updating a baseline page's content only affects that staged run's view."""
    page = await _make_page(baseline_db, "headline")
    original_content = page.content
    new_content = "staged edit"

    await staged_db.update_page_content(page.id, new_content=new_content)

    staged_page = await staged_db.get_page(page.id)
    assert staged_page is not None
    assert staged_page.content == new_content

    obs_page = await observer_db.get_page(page.id)
    assert obs_page is not None
    assert obs_page.content == original_content


async def test_retroactively_staged_update_page_content_visible_to_staged_reader(project_id):
    """After retroactive staging, a staged reader still sees the updated content via event replay."""
    baseline = await _make_db(project_id, staged=False)
    await baseline.init_budget(100)
    run_db = await _make_db(project_id, staged=False)
    await run_db.init_budget(100)
    helper = await _make_db(project_id, staged=False)

    page = await _make_page(baseline, "headline")
    original_content = page.content
    new_content = "run-written content"

    await run_db.update_page_content(page.id, new_content=new_content)

    await helper.stage_run(run_db.run_id)

    staged_reader = await DB.create(run_id=run_db.run_id, staged=True)
    staged_reader.project_id = project_id
    reader_page = await staged_reader.get_page(page.id)
    assert reader_page is not None
    assert reader_page.content == new_content

    observer = await _make_db(project_id, staged=False)
    obs_page = await observer.get_page(page.id)
    assert obs_page is not None
    assert obs_page.content == original_content

    await baseline.delete_run_data()
    await run_db.delete_run_data()
    await helper.delete_run_data()
    await observer.delete_run_data()

async def test_view_as_staged_sees_staged_pages(baseline_db, staged_db, observer_db):
    """A view built from a non-staged DB onto a staged run_id sees that run's pages.

    This is the visibility property the trace-tree endpoint relies on when
    rendering staged runs (e.g. versus judgments) without opening a fresh
    Supabase client just to flip the staging flags.
    """
    baseline_page = await _make_page(baseline_db, "baseline claim")
    staged_page = await _make_page(staged_db, "staged claim")

    view = observer_db.view_as_staged(staged_db.run_id)

    view_page = await view.get_page(staged_page.id)
    assert view_page is not None
    assert view_page.headline == "staged claim"

    view_baseline = await view.get_page(baseline_page.id)
    assert view_baseline is not None

    observer_staged = await observer_db.get_page(staged_page.id)
    assert observer_staged is None, (
        "observer_db (non-staged) must not see staged pages; view_as_staged is "
        "what makes them visible"
    )
