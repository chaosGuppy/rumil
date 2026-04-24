"""Non-LLM tests for DB.set_page_hidden + the three spec-editing moves.

Covers:
- ``DB.set_page_hidden`` flips the flag, records a mutation event, and
  composes correctly with staged-run visibility.
- ``supersede_spec_item`` creates a replacement SPEC_ITEM and supersedes the old.
- ``delete_spec_item`` drops a spec item from the current spec by removing
  its SPEC_OF link; the page itself remains as an orphan.
- ``finalize_artefact`` flips the latest artefact's hidden flag to False.
- Each move rejects misconfigured calls early.
"""

import uuid

import pytest_asyncio

from rumil.database import DB
from rumil.models import (
    Call,
    CallStatus,
    CallType,
    LinkType,
    Page,
    PageLayer,
    PageLink,
    PageType,
    Workspace,
)
from rumil.moves.delete_spec_item import DeleteSpecItemPayload
from rumil.moves.delete_spec_item import execute as delete_spec_item
from rumil.moves.finalize_artefact import FinalizeArtefactPayload
from rumil.moves.finalize_artefact import execute as finalize_artefact
from rumil.moves.supersede_spec_item import SupersedeSpecItemPayload
from rumil.moves.supersede_spec_item import execute as supersede_spec_item


async def _make_page(
    db: DB,
    headline: str,
    *,
    page_type: PageType = PageType.CLAIM,
    hidden: bool = False,
) -> Page:
    page = Page(
        page_type=page_type,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=f"Content for: {headline}",
        headline=headline,
        hidden=hidden,
    )
    await db.save_page(page)
    return page


async def _make_task(db: DB, headline: str = "an artefact task") -> Page:
    return await _make_page(db, headline, page_type=PageType.QUESTION, hidden=True)


async def _link_spec(db: DB, spec: Page, task: Page) -> PageLink:
    link = PageLink(
        from_page_id=spec.id,
        to_page_id=task.id,
        link_type=LinkType.SPEC_OF,
    )
    await db.save_link(link)
    return link


async def _make_call(db: DB, scope_id: str | None, call_type: CallType) -> Call:
    call = Call(
        call_type=call_type,
        workspace=Workspace.RESEARCH,
        scope_page_id=scope_id,
        status=CallStatus.PENDING,
    )
    await db.save_call(call)
    return call


async def test_set_page_hidden_flips_flag_and_roundtrips(tmp_db):
    page = await _make_page(tmp_db, "a page", hidden=False)

    await tmp_db.set_page_hidden(page.id, True)
    refreshed = await tmp_db.get_page(page.id)
    assert refreshed is not None
    assert refreshed.hidden is True

    await tmp_db.set_page_hidden(page.id, False)
    refreshed = await tmp_db.get_page(page.id)
    assert refreshed is not None
    assert refreshed.hidden is False


async def test_set_page_hidden_records_mutation_event(tmp_db):
    page = await _make_page(tmp_db, "audit target", hidden=False)
    await tmp_db.set_page_hidden(page.id, True)

    rows = (
        await tmp_db._execute(
            tmp_db.client.table("mutation_events")
            .select("event_type, target_id, payload")
            .eq("target_id", page.id)
        )
    ).data
    events = [r for r in (rows or []) if r["event_type"] == "set_hidden"]
    assert len(events) == 1
    assert events[0]["payload"]["hidden"] is True


@pytest_asyncio.fixture
async def shared_project():
    setup_db = await DB.create(run_id=str(uuid.uuid4()))
    project = await setup_db.get_or_create_project(f"test-set-hidden-staged-{uuid.uuid4().hex[:8]}")
    yield project.id
    await setup_db._execute(setup_db.client.table("projects").delete().eq("id", project.id))


async def test_set_page_hidden_composes_with_staged_runs(shared_project):
    """A staged flip is visible to the staged reader only; baseline row
    stays put, and a non-staged observer sees the baseline value."""
    baseline = await DB.create(run_id=str(uuid.uuid4()), staged=False)
    baseline.project_id = shared_project
    await baseline.init_budget(10)

    staged = await DB.create(run_id=str(uuid.uuid4()), staged=True)
    staged.project_id = shared_project

    observer = await DB.create(run_id=str(uuid.uuid4()), staged=False)
    observer.project_id = shared_project

    try:
        page = await _make_page(baseline, "hidden-flip target", hidden=False)

        await staged.set_page_hidden(page.id, True)

        staged_view = await staged.get_page(page.id)
        assert staged_view is not None
        assert staged_view.hidden is True

        observer_view = await observer.get_page(page.id)
        assert observer_view is not None
        assert observer_view.hidden is False
    finally:
        await baseline.delete_run_data()
        await staged.delete_run_data()
        await observer.close()


async def test_supersede_spec_item_creates_replacement_and_supersedes_old(tmp_db):
    task = await _make_task(tmp_db)
    old = await _make_page(tmp_db, "old rule", page_type=PageType.SPEC_ITEM, hidden=True)
    await _link_spec(tmp_db, old, task)
    call = await _make_call(tmp_db, task.id, CallType.GENERATE_SPEC)

    payload = SupersedeSpecItemPayload(
        old_id=old.id,
        headline="Tighter rule",
        content="Be more specific about X.",
    )
    result = await supersede_spec_item(payload, call, tmp_db)
    assert result.created_page_id is not None

    new = await tmp_db.get_page(result.created_page_id)
    assert new is not None
    assert new.page_type == PageType.SPEC_ITEM
    assert new.hidden is True

    refreshed_old = await tmp_db.get_page(old.id)
    assert refreshed_old is not None
    assert refreshed_old.is_superseded is True
    assert refreshed_old.superseded_by == new.id

    links = await tmp_db.get_links_from(new.id)
    spec_of = [l for l in links if l.link_type == LinkType.SPEC_OF]
    assert len(spec_of) == 1
    assert spec_of[0].to_page_id == task.id


async def test_supersede_spec_item_errors_when_target_is_not_a_spec_item(tmp_db):
    task = await _make_task(tmp_db)
    not_a_spec = await _make_page(tmp_db, "plain claim", page_type=PageType.CLAIM)
    call = await _make_call(tmp_db, task.id, CallType.GENERATE_SPEC)

    payload = SupersedeSpecItemPayload(
        old_id=not_a_spec.id,
        headline="Won't be created",
        content="Nope.",
    )
    result = await supersede_spec_item(payload, call, tmp_db)
    assert result.created_page_id is None
    assert "spec item" in result.message.lower()


async def test_supersede_spec_item_rejects_spec_from_another_task(tmp_db):
    """Scope guard: supersede_spec_item must not mutate a spec item that belongs
    to a different task's spec — doing so would silently shrink the other
    task's spec without surfacing an error."""
    this_task = await _make_task(tmp_db, "this task")
    other_task = await _make_task(tmp_db, "other task")

    other_spec = await _make_page(
        tmp_db, "belongs to other task", page_type=PageType.SPEC_ITEM, hidden=True
    )
    await _link_spec(tmp_db, other_spec, other_task)

    call = await _make_call(tmp_db, this_task.id, CallType.GENERATE_SPEC)
    payload = SupersedeSpecItemPayload(
        old_id=other_spec.id,
        headline="Wrong task",
        content="Should be rejected.",
    )
    result = await supersede_spec_item(payload, call, tmp_db)
    assert result.created_page_id is None
    assert "not part of this" in result.message.lower()

    refreshed_other = await tmp_db.get_page(other_spec.id)
    assert refreshed_other is not None
    assert refreshed_other.is_superseded is False

    other_links = await tmp_db.get_links_from(other_spec.id)
    still_linked = [
        l
        for l in other_links
        if l.link_type == LinkType.SPEC_OF and l.to_page_id == other_task.id
    ]
    assert len(still_linked) == 1


async def test_supersede_spec_item_errors_when_scope_is_not_a_question(tmp_db):
    some_claim = await _make_page(tmp_db, "random", page_type=PageType.CLAIM)
    spec = await _make_page(tmp_db, "a spec", page_type=PageType.SPEC_ITEM, hidden=True)
    call = await _make_call(tmp_db, some_claim.id, CallType.GENERATE_SPEC)

    payload = SupersedeSpecItemPayload(
        old_id=spec.id,
        headline="Won't happen",
        content="Nope.",
    )
    result = await supersede_spec_item(payload, call, tmp_db)
    assert result.created_page_id is None
    assert "question" in result.message.lower()


async def test_delete_spec_item_removes_spec_of_link_but_keeps_page(tmp_db):
    task = await _make_task(tmp_db)
    spec = await _make_page(tmp_db, "soon-deleted", page_type=PageType.SPEC_ITEM, hidden=True)
    await _link_spec(tmp_db, spec, task)
    call = await _make_call(tmp_db, task.id, CallType.GENERATE_SPEC)

    result = await delete_spec_item(DeleteSpecItemPayload(spec_id=spec.id), call, tmp_db)
    assert result.created_page_id is None
    assert "deleted" in result.message.lower()

    still_there = await tmp_db.get_page(spec.id)
    assert still_there is not None

    links = await tmp_db.get_links_from(spec.id)
    spec_of = [l for l in links if l.link_type == LinkType.SPEC_OF and l.to_page_id == task.id]
    assert spec_of == []


async def test_delete_spec_item_noop_when_already_not_in_spec(tmp_db):
    task = await _make_task(tmp_db)
    orphan_spec = await _make_page(tmp_db, "orphan", page_type=PageType.SPEC_ITEM, hidden=True)
    call = await _make_call(tmp_db, task.id, CallType.GENERATE_SPEC)

    result = await delete_spec_item(DeleteSpecItemPayload(spec_id=orphan_spec.id), call, tmp_db)
    assert result.created_page_id is None
    assert "already" in result.message.lower() or "nothing" in result.message.lower()


async def test_delete_spec_item_errors_when_target_is_not_a_spec_item(tmp_db):
    task = await _make_task(tmp_db)
    not_a_spec = await _make_page(tmp_db, "claim", page_type=PageType.CLAIM)
    call = await _make_call(tmp_db, task.id, CallType.GENERATE_SPEC)

    result = await delete_spec_item(DeleteSpecItemPayload(spec_id=not_a_spec.id), call, tmp_db)
    assert result.created_page_id is None
    assert "spec item" in result.message.lower()


async def test_finalize_artefact_flips_latest_artefact_visible(tmp_db):
    task = await _make_task(tmp_db)
    artefact = await _make_page(tmp_db, "final artefact", page_type=PageType.ARTEFACT, hidden=True)
    await tmp_db.save_link(
        PageLink(
            from_page_id=artefact.id,
            to_page_id=task.id,
            link_type=LinkType.ARTEFACT_OF,
        )
    )
    call = await _make_call(tmp_db, task.id, CallType.GENERATE_SPEC)

    result = await finalize_artefact(FinalizeArtefactPayload(note="converged"), call, tmp_db)
    assert "Finalized" in result.message

    refreshed = await tmp_db.get_page(artefact.id)
    assert refreshed is not None
    assert refreshed.hidden is False


async def test_finalize_artefact_errors_when_no_artefact(tmp_db):
    task = await _make_task(tmp_db)
    call = await _make_call(tmp_db, task.id, CallType.GENERATE_SPEC)

    result = await finalize_artefact(FinalizeArtefactPayload(), call, tmp_db)
    assert result.created_page_id is None
    assert "no artefact" in result.message.lower()


async def test_finalize_artefact_is_idempotent_when_already_visible(tmp_db):
    task = await _make_task(tmp_db)
    visible_artefact = await _make_page(
        tmp_db, "already live", page_type=PageType.ARTEFACT, hidden=False
    )
    await tmp_db.save_link(
        PageLink(
            from_page_id=visible_artefact.id,
            to_page_id=task.id,
            link_type=LinkType.ARTEFACT_OF,
        )
    )
    call = await _make_call(tmp_db, task.id, CallType.GENERATE_SPEC)

    result = await finalize_artefact(FinalizeArtefactPayload(), call, tmp_db)
    assert "already visible" in result.message.lower()

    refreshed = await tmp_db.get_page(visible_artefact.id)
    assert refreshed is not None
    assert refreshed.hidden is False


async def test_finalize_artefact_errors_when_scope_is_not_a_question(tmp_db):
    some_claim = await _make_page(tmp_db, "random", page_type=PageType.CLAIM)
    call = await _make_call(tmp_db, some_claim.id, CallType.GENERATE_SPEC)

    result = await finalize_artefact(FinalizeArtefactPayload(), call, tmp_db)
    assert result.created_page_id is None
    assert "question" in result.message.lower()
