"""Test page-scope visibility: optional per-row restriction to a question.

A page (or link) tagged with ``scope_question_id = Q`` is only visible to
DB instances that have been scoped to Q. Untagged rows are visible to
every DB.

Mirrors the staged-runs test layout — see ``test_staged_runs.py`` for the
pattern.
"""

import uuid

import pytest_asyncio

from rumil.calls.prioritization import run_prioritization_call
from rumil.calls.stages import (
    CallInfra,
    CallRunner,
    ClosingReviewer,
    ContextBuilder,
    ContextResult,
    UpdateResult,
    WorkspaceUpdater,
)
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


async def _make_db(project_id: str, scope_question_id: str | None = None) -> DB:
    db = await DB.create(run_id=str(uuid.uuid4()), scope_question_id=scope_question_id)
    db.project_id = project_id
    return db


async def _make_page(
    db: DB,
    headline: str,
    page_type: PageType = PageType.CLAIM,
    scope_question_id: str | None = None,
) -> Page:
    page = Page(
        page_type=page_type,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=f"Content for: {headline}",
        headline=headline,
        scope_question_id=scope_question_id,
    )
    await db.save_page(page)
    return page


async def _make_question(db: DB, headline: str) -> Page:
    return await _make_page(db, headline, page_type=PageType.QUESTION)


async def _link(
    db: DB,
    from_page: Page,
    to_page: Page,
    scope_question_id: str | None = None,
) -> PageLink:
    link = PageLink(
        from_page_id=from_page.id,
        to_page_id=to_page.id,
        link_type=LinkType.CONSIDERATION,
        strength=5.0,
        reasoning="test link",
        scope_question_id=scope_question_id,
    )
    await db.save_link(link)
    return link


@pytest_asyncio.fixture
async def project_id():
    db = await DB.create(run_id=str(uuid.uuid4()))
    project = await db.get_or_create_project(f"test-scopes-{uuid.uuid4().hex[:8]}")
    try:
        yield project.id
    finally:
        await db._execute(db.client.table("projects").delete().eq("id", project.id))
        await db.close()


@pytest_asyncio.fixture
async def writer_db(project_id):
    """An unscoped DB used to write rows with explicit scope_question_id values."""
    db = await _make_db(project_id)
    await db.init_budget(100)
    try:
        yield db
    finally:
        await db.delete_run_data()
        await db.close()


async def test_unscoped_page_visible_to_every_db(writer_db, project_id):
    """A page with NULL scope is visible to every reader regardless of their scope."""
    q1 = await _make_question(writer_db, "scope question 1")
    p = await _make_page(writer_db, "unscoped claim", scope_question_id=None)

    unscoped_reader = await _make_db(project_id, scope_question_id=None)
    scoped_reader = await _make_db(project_id, scope_question_id=q1.id)
    try:
        assert await unscoped_reader.get_page(p.id) is not None
        assert await scoped_reader.get_page(p.id) is not None
    finally:
        await unscoped_reader.close()
        await scoped_reader.close()


async def test_scoped_page_invisible_to_mismatched_scope(writer_db, project_id):
    """A page scoped to Q1 is invisible to a reader scoped to Q2."""
    q1 = await _make_question(writer_db, "scope question 1")
    q2 = await _make_question(writer_db, "scope question 2")
    p = await _make_page(writer_db, "Q1-only claim", scope_question_id=q1.id)

    reader_q2 = await _make_db(project_id, scope_question_id=q2.id)
    try:
        assert await reader_q2.get_page(p.id) is None
    finally:
        await reader_q2.close()


async def test_scoped_page_visible_to_matching_scope(writer_db, project_id):
    """A page scoped to Q1 is visible to a reader scoped to Q1."""
    q1 = await _make_question(writer_db, "scope question 1")
    p = await _make_page(writer_db, "Q1-only claim", scope_question_id=q1.id)

    reader_q1 = await _make_db(project_id, scope_question_id=q1.id)
    try:
        assert await reader_q1.get_page(p.id) is not None
    finally:
        await reader_q1.close()


async def test_scoped_page_visible_to_unscoped_reader(writer_db, project_id):
    """A page scoped to Q1 is visible to an unscoped DB (the default).

    Unscoped DBs see everything — only scoped DBs apply the filter. This
    is the load-bearing semantic that keeps the API/CLI/orchestrator
    paths unaffected.
    """
    q1 = await _make_question(writer_db, "scope question 1")
    p = await _make_page(writer_db, "Q1-only claim", scope_question_id=q1.id)

    unscoped_reader = await _make_db(project_id, scope_question_id=None)
    try:
        assert await unscoped_reader.get_page(p.id) is not None
    finally:
        await unscoped_reader.close()


async def test_get_pages_by_ids_respects_scope(writer_db, project_id):
    q1 = await _make_question(writer_db, "scope question 1")
    q2 = await _make_question(writer_db, "scope question 2")
    p_q1 = await _make_page(writer_db, "Q1 claim", scope_question_id=q1.id)
    p_q2 = await _make_page(writer_db, "Q2 claim", scope_question_id=q2.id)
    p_open = await _make_page(writer_db, "unscoped claim", scope_question_id=None)

    reader_q1 = await _make_db(project_id, scope_question_id=q1.id)
    try:
        pages = await reader_q1.get_pages_by_ids([p_q1.id, p_q2.id, p_open.id])
        assert p_q1.id in pages
        assert p_open.id in pages
        assert p_q2.id not in pages
    finally:
        await reader_q1.close()


async def test_scoped_link_invisible_to_mismatched_scope(writer_db, project_id):
    """Links carry scope_question_id too; same visibility semantics."""
    q1 = await _make_question(writer_db, "scope question 1")
    q2 = await _make_question(writer_db, "scope question 2")
    a = await _make_page(writer_db, "claim a")
    b = await _make_page(writer_db, "claim b")
    link = await _link(writer_db, a, b, scope_question_id=q1.id)

    reader_q1 = await _make_db(project_id, scope_question_id=q1.id)
    reader_q2 = await _make_db(project_id, scope_question_id=q2.id)
    unscoped = await _make_db(project_id, scope_question_id=None)
    try:
        assert await reader_q1.get_link(link.id) is not None
        assert await reader_q2.get_link(link.id) is None
        assert await unscoped.get_link(link.id) is not None

        ids_q1 = {ln.id for ln in await reader_q1.get_links_from(a.id)}
        ids_q2 = {ln.id for ln in await reader_q2.get_links_from(a.id)}
        ids_unscoped = {ln.id for ln in await unscoped.get_links_from(a.id)}
        assert link.id in ids_q1
        assert link.id not in ids_q2
        assert link.id in ids_unscoped

        ids_q1_to = {ln.id for ln in await reader_q1.get_links_to(b.id)}
        ids_q2_to = {ln.id for ln in await reader_q2.get_links_to(b.id)}
        assert link.id in ids_q1_to
        assert link.id not in ids_q2_to
    finally:
        await reader_q1.close()
        await reader_q2.close()
        await unscoped.close()


async def test_get_root_questions_rpc_respects_scope(writer_db, project_id):
    """The get_root_questions RPC applies the scope predicate in SQL."""
    open_root = await _make_question(writer_db, "open root")
    scope_anchor = await _make_question(writer_db, "scope anchor")
    scoped_root = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="scoped root content",
        headline="scoped root",
        scope_question_id=scope_anchor.id,
    )
    await writer_db.save_page(scoped_root)

    reader_anchor = await _make_db(project_id, scope_question_id=scope_anchor.id)
    other_anchor = await _make_question(writer_db, "other anchor")
    reader_other = await _make_db(project_id, scope_question_id=other_anchor.id)
    try:
        roots_anchor = {q.id for q in await reader_anchor.get_root_questions()}
        roots_other = {q.id for q in await reader_other.get_root_questions()}

        assert open_root.id in roots_anchor
        assert scoped_root.id in roots_anchor

        assert open_root.id in roots_other
        assert scoped_root.id not in roots_other
    finally:
        await reader_anchor.close()
        await reader_other.close()


async def test_with_scope_returns_sibling_with_shared_client(writer_db, project_id):
    """``with_scope`` flips the scope on a sibling DB without forking the HTTP client."""
    q1 = await _make_question(writer_db, "q1")
    p_q1 = await _make_page(writer_db, "Q1 claim", scope_question_id=q1.id)

    sibling = writer_db.with_scope(q1.id)
    assert sibling.client is writer_db.client
    assert sibling.scope_question_id == q1.id
    assert await sibling.get_page(p_q1.id) is not None

    cleared = sibling.with_scope(None)
    assert cleared.scope_question_id is None
    assert await cleared.get_page(p_q1.id) is not None


async def test_fork_preserves_scope_by_default(writer_db, project_id):
    """``fork()`` without args inherits the parent's scope; pass None to clear it."""
    q1 = await _make_question(writer_db, "q1")
    scoped = writer_db.with_scope(q1.id)

    inherited = await scoped.fork()
    try:
        assert inherited.scope_question_id == q1.id
    finally:
        await inherited.close()

    cleared = await scoped.fork(scope_question_id=None)
    try:
        assert cleared.scope_question_id is None
    finally:
        await cleared.close()


class _CapturingContextBuilder(ContextBuilder):
    async def build_context(self, infra: CallInfra) -> ContextResult:
        captured["context_db_scope"] = infra.db.scope_question_id
        return ContextResult(context_text="", working_page_ids=[])


class _NoopWorkspaceUpdater(WorkspaceUpdater):
    async def update_workspace(self, infra: CallInfra, context: ContextResult) -> UpdateResult:
        captured["update_db_scope"] = infra.db.scope_question_id
        return UpdateResult(created_page_ids=[], moves=[], all_loaded_ids=[])


class _NoopClosingReviewer(ClosingReviewer):
    async def closing_review(
        self,
        infra: CallInfra,
        context: ContextResult,
        creation: UpdateResult,
    ) -> None:
        captured["closing_db_scope"] = infra.db.scope_question_id


captured: dict[str, str | None] = {}


class _ScopeProbeRunner(CallRunner):
    context_builder_cls = _CapturingContextBuilder
    workspace_updater_cls = _NoopWorkspaceUpdater
    closing_reviewer_cls = _NoopClosingReviewer
    call_type = CallType.FIND_CONSIDERATIONS

    def task_description(self) -> str:
        return "probe"


async def test_call_runner_forks_db_into_scoped_mode(writer_db):
    """CallRunner.run() forks the DB so each stage sees ``scope_question_id == question_id``."""
    captured.clear()
    question = await _make_question(writer_db, "scope target question")
    call = Call(
        call_type=CallType.FIND_CONSIDERATIONS,
        workspace=Workspace.RESEARCH,
        scope_page_id=question.id,
        status=CallStatus.PENDING,
    )
    await writer_db.save_call(call)

    runner = _ScopeProbeRunner(question.id, call, writer_db)
    await runner.run()

    assert captured["context_db_scope"] == question.id
    assert captured["update_db_scope"] == question.id
    assert captured["closing_db_scope"] == question.id


async def test_run_prioritization_call_enters_scoped_mode(writer_db, mocker):
    """run_prioritization_call narrows the DB scope to ``call.scope_page_id``."""
    question = await _make_question(writer_db, "prio target question")
    call = Call(
        call_type=CallType.PRIORITIZATION,
        workspace=Workspace.PRIORITIZATION,
        scope_page_id=question.id,
        status=CallStatus.PENDING,
        budget_allocated=5,
    )
    await writer_db.save_call(call)

    seen: dict[str, str | None] = {}

    async def _stub_run_single_call(*args, **kwargs):
        seen["scope"] = kwargs["db"].scope_question_id
        from rumil.calls.common import RunCallResult

        return RunCallResult()

    mocker.patch(
        "rumil.calls.prioritization.run_single_call",
        side_effect=_stub_run_single_call,
    )

    await run_prioritization_call(
        task_description="probe",
        context_text="",
        call=call,
        db=writer_db,
        system_prompt="probe system",
    )

    assert seen["scope"] == question.id
    assert writer_db.scope_question_id is None
