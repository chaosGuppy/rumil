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
