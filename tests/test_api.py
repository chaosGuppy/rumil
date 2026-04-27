"""Tests for the API endpoints."""

import pytest
from httpx import ASGITransport, AsyncClient

from rumil.api.app import app, require_admin
from rumil.api.auth import AuthUser, get_current_user
from rumil.database import DB


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
