"""Tests for the API endpoints."""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from rumil.api.app import app, require_admin
from rumil.api.auth import AuthUser, get_current_user
from rumil.database import DB
from rumil.models import (
    Call,
    CallStatus,
    CallType,
    Page,
    PageLayer,
    PageType,
    Workspace,
)


@pytest.fixture
def api_client():
    user = AuthUser(user_id="", email="test@example.com")
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[require_admin] = lambda: user
    transport = ASGITransport(app=app)
    client = AsyncClient(transport=transport, base_url="http://test")
    try:
        yield client
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(require_admin, None)


@pytest.mark.asyncio
async def test_list_projects(api_client):
    resp = await api_client.get("/api/projects")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


async def test_llm_exchange_with_null_round(api_client, tmp_db, scout_call):
    """Exchanges without a round (e.g. closing review) should serialize cleanly."""
    exchange_id = await tmp_db.save_llm_exchange(
        call_id=scout_call.id,
        phase="review",
        system_prompt="test",
        user_message="test",
        response_text="test",
        tool_calls=[],
        round_num=None,
    )

    resp = await api_client.get(f"/api/llm-exchanges/{exchange_id}")
    assert resp.status_code == 200
    assert resp.json()["round"] is None

    resp = await api_client.get(f"/api/calls/{scout_call.id}/llm-exchanges")
    assert resp.status_code == 200
    assert any(e["round"] is None for e in resp.json())


# ---------------------------------------------------------------------------
# Regression tests for DB connection lifecycle.
#
# Before chaosGuppy/rumil#274, api/app.py created a new DB per request via
# _get_db() but never called db.close(). Under any meaningful load this
# leaks HTTP connections. These tests spy on DB.close and assert it is
# called exactly as often as DB.create for each endpoint hit.
#
# Written against pre-refactor main: they must fail there (close never
# called) and pass after the yield-based dependency refactor.
# ---------------------------------------------------------------------------


@pytest.fixture
def db_close_spy(mocker):
    """Spy on DB.create and DB.close. Returns a helper that reports
    the delta in each since the spy was installed, so tests can make
    assertions that are robust to any DB instances created/closed by
    test setup itself (e.g. by the tmp_db fixture)."""
    create_spy = mocker.spy(DB, "create")
    close_spy = mocker.spy(DB, "close")

    start_created = create_spy.call_count
    start_closed = close_spy.call_count

    class Delta:
        @property
        def created(self) -> int:
            return create_spy.call_count - start_created

        @property
        def closed(self) -> int:
            return close_spy.call_count - start_closed

    return Delta()


async def test_single_request_closes_its_db(api_client, db_close_spy):
    """A single API request must create exactly one DB and close it."""
    resp = await api_client.get("/api/projects")
    assert resp.status_code == 200
    assert db_close_spy.created == 1, f"expected 1 DB.create call, got {db_close_spy.created}"
    assert db_close_spy.closed == 1, f"expected 1 DB.close call, got {db_close_spy.closed}"


async def test_multiple_requests_close_all_dbs(api_client, db_close_spy):
    """Every request closes its DB. 5 requests -> 5 creates, 5 closes.

    Before #274 this fails with close==0 because _get_db never calls
    db.close() and nothing else picks up the slack."""
    for _ in range(5):
        resp = await api_client.get("/api/projects")
        assert resp.status_code == 200

    assert db_close_spy.created == 5
    assert db_close_spy.closed == 5


async def test_staged_run_endpoint_closes_its_db(api_client, db_close_spy):
    """Endpoints that accept staged_run_id go through _get_db_maybe_staged.
    That path must also close cleanly."""
    # Unknown page_id — endpoint returns 404, but the DB should still close.
    resp = await api_client.get(
        "/api/pages/00000000-0000-0000-0000-000000000000?staged_run_id=test-run-does-not-exist"
    )
    # 404 is fine; the lifecycle must still run.
    assert resp.status_code in (200, 404)
    assert db_close_spy.created == 1
    assert db_close_spy.closed == 1


async def test_db_closed_even_when_endpoint_raises_http_exception(
    api_client,
    db_close_spy,
):
    """When an endpoint raises HTTPException (e.g. 404), the dependency
    cleanup must still run. This is the fundamental FastAPI
    yield-dependency guarantee."""
    resp = await api_client.get("/api/pages/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404
    assert db_close_spy.created == 1
    assert db_close_spy.closed == 1


async def test_trace_tree_surfaces_staged_question_and_scope(api_client):
    """Trace-tree for a staged run must return the Question page and scope
    summaries, not nulls. Regression for the versus case where staged runs'
    trace-tree responses came back with empty scope_page_summary because the
    endpoint read through a baseline-only DB.
    """
    run_id = str(uuid.uuid4())
    staged = await DB.create(run_id=run_id, staged=True)
    project = await staged.get_or_create_project(f"test-tt-{run_id[:8]}")
    staged.project_id = project.id
    await staged.init_budget(100)

    try:
        question = Page(
            page_type=PageType.QUESTION,
            layer=PageLayer.SQUIDGY,
            workspace=Workspace.RESEARCH,
            content="Does view_as_staged surface staged pages to the trace-tree?",
            headline="view_as_staged trace-tree question",
        )
        await staged.save_page(question)

        call = Call(
            call_type=CallType.FIND_CONSIDERATIONS,
            workspace=Workspace.RESEARCH,
            scope_page_id=question.id,
            status=CallStatus.COMPLETE,
        )
        await staged.save_call(call)

        await staged.create_run(
            name="test staged trace-tree",
            question_id=question.id,
            config={},
        )

        resp = await api_client.get(f"/api/runs/{run_id}/trace-tree")
        assert resp.status_code == 200
        body = resp.json()

        assert body["question"] is not None, (
            "staged run's Question page came back null; view_as_staged not applied"
        )
        assert body["question"]["id"] == question.id
        assert body["question"]["headline"] == "view_as_staged trace-tree question"

        scoped_nodes = [n for n in body["calls"] if n["call"]["scope_page_id"] == question.id]
        assert scoped_nodes, "expected a call scoped to the staged question"
        assert scoped_nodes[0]["scope_page_summary"], (
            "scope_page_summary came back empty; staged page not visible to trace-tree reads"
        )
    finally:
        await staged.delete_run_data(delete_project=True)
        await staged.close()


async def test_trace_tree_staged_run_does_not_open_extra_db(api_client, db_close_spy):
    """The staged-run path must not open a fresh Supabase client just to flip
    the staging flags. view_as_staged reuses the dependency DB's client, so
    exactly one DB should be created and closed per request."""
    run_id = str(uuid.uuid4())
    staged = await DB.create(run_id=run_id, staged=True)
    project = await staged.get_or_create_project(f"test-tt2-{run_id[:8]}")
    staged.project_id = project.id
    await staged.init_budget(100)

    try:
        question = Page(
            page_type=PageType.QUESTION,
            layer=PageLayer.SQUIDGY,
            workspace=Workspace.RESEARCH,
            content="q",
            headline="q",
        )
        await staged.save_page(question)
        await staged.create_run(name="tt", question_id=question.id, config={})

        created_before_request = db_close_spy.created
        closed_before_request = db_close_spy.closed

        resp = await api_client.get(f"/api/runs/{run_id}/trace-tree")
        assert resp.status_code == 200

        request_created = db_close_spy.created - created_before_request
        request_closed = db_close_spy.closed - closed_before_request
        assert request_created == 1, (
            f"expected 1 DB.create for the request, got {request_created} — "
            "did the staged path reintroduce a fresh client?"
        )
        assert request_closed == 1
    finally:
        await staged.delete_run_data(delete_project=True)
        await staged.close()


async def test_create_run_persists_entrypoint(tmp_db):
    """create_run should round-trip the entrypoint tag through get_run."""
    await tmp_db.create_run(
        name="ep-roundtrip",
        question_id=None,
        config={},
        entrypoint="run_call",
    )
    row = await tmp_db.get_run(tmp_db.run_id)
    assert row is not None
    assert row.get("entrypoint") == "run_call"


async def test_create_run_default_entrypoint_is_null(tmp_db):
    """Existing call sites that don't pass entrypoint should leave it NULL."""
    await tmp_db.create_run(name="legacy", question_id=None, config={})
    row = await tmp_db.get_run(tmp_db.run_id)
    assert row is not None
    assert row.get("entrypoint") is None


async def test_list_run_call_experiments_filters_by_entrypoint(tmp_db):
    """Only rows with entrypoint='run_call' in this project should come back."""
    project_id = tmp_db.project_id
    await tmp_db.create_run(
        name="visible",
        question_id=None,
        config={"model": "test-model"},
        entrypoint="run_call",
    )

    # Untagged sibling run in the same project should be ignored.
    other_run_id = str(uuid.uuid4())
    other = await DB.create(run_id=other_run_id, project_id=str(project_id))
    other.project_id = project_id
    await other.create_run(name="untagged", question_id=None, config={})

    # A run_call run in a different project should not leak in.
    foreign_run_id = str(uuid.uuid4())
    foreign = await DB.create(run_id=foreign_run_id)
    foreign_project = await foreign.get_or_create_project(f"test-foreign-{foreign_run_id[:8]}")
    foreign.project_id = foreign_project.id
    try:
        await foreign.create_run(
            name="foreign",
            question_id=None,
            config={},
            entrypoint="run_call",
        )

        rows = await tmp_db.list_run_call_experiments()
        names = {r["name"] for r in rows}
        assert "visible" in names
        assert "untagged" not in names
        assert "foreign" not in names
    finally:
        await foreign.delete_run_data(delete_project=True)
        await foreign.close()
        # Drop the sibling run so tmp_db's project teardown doesn't FK-fail.
        await other.delete_run_data()
        await other.close()


async def test_list_experiments_merges_kinds_sorted_desc(api_client, tmp_db):
    """/api/experiments returns ab_eval and run_call rows together, newest first."""
    project_id = str(tmp_db.project_id)

    # Older run_call row.
    older_run_id = str(uuid.uuid4())
    older = await DB.create(run_id=older_run_id, project_id=project_id)
    older.project_id = tmp_db.project_id
    await older.create_run(
        name="older-run-call",
        question_id=None,
        config={"model": "haiku"},
        entrypoint="run_call",
    )

    # AB eval report (auto-stamped created_at, will be later).
    report_id = await tmp_db.save_ab_eval_report(
        run_id_a=str(uuid.uuid4()),
        run_id_b=str(uuid.uuid4()),
        question_id_a="",
        question_id_b="",
        overall_assessment="this is the assessment body",
        dimension_reports=[
            {"name": "clarity", "display_name": "Clarity", "preference": "A slightly", "report": ""}
        ],
    )

    resp = await api_client.get(f"/api/experiments?project_id={project_id}")
    assert resp.status_code == 200
    items = resp.json()

    kinds = [it["kind"] for it in items]
    assert "ab_eval" in kinds
    assert "run_call" in kinds

    # Sorted desc by created_at: ab_eval (just inserted) should precede the run_call.
    ab = next(it for it in items if it["kind"] == "ab_eval")
    rc = next(it for it in items if it["kind"] == "run_call" and it["name"] == "older-run-call")
    assert ab["created_at"] >= rc["created_at"]
    assert items.index(ab) < items.index(rc)

    # Run-call row exposes the trace-link fields the frontend uses.
    assert rc["run_id"] == older_run_id
    assert rc["config_summary"].get("model") == "haiku"

    # AB eval row preserves its identity for the detail-page link.
    assert ab["id"] == report_id

    # Drop the sibling run + report so tmp_db's project teardown doesn't FK-fail.
    await tmp_db._execute(tmp_db.client.table("ab_eval_reports").delete().eq("id", report_id))
    await older.delete_run_data()
    await older.close()


async def test_list_experiments_requires_admin(api_client, monkeypatch):
    """Non-admin users get 403 from /api/experiments."""
    from fastapi import HTTPException

    from rumil.api.app import app, require_admin

    def deny():
        raise HTTPException(status_code=403, detail="not admin")

    app.dependency_overrides[require_admin] = deny
    try:
        resp = await api_client.get("/api/experiments")
        assert resp.status_code == 403
    finally:
        # Restore the admin override the api_client fixture installed.
        from rumil.api.auth import AuthUser

        app.dependency_overrides[require_admin] = lambda: AuthUser(
            user_id="", email="test@example.com"
        )
