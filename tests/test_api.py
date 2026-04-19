"""Tests for the API endpoints."""

import pytest
from httpx import ASGITransport, AsyncClient

from rumil.api.app import app
from rumil.database import DB


@pytest.fixture
def api_client():
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


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


async def test_stage_run_missing_returns_404(api_client, mocker):
    """POST /api/runs/{run_id}/stage returns 404 when the run does not exist."""
    mocker.patch.object(DB, "get_run", return_value=None)
    stage_spy = mocker.patch.object(DB, "stage_run")

    resp = await api_client.post("/api/runs/does-not-exist/stage")
    assert resp.status_code == 404
    stage_spy.assert_not_called()


async def test_stage_run_already_staged_returns_409(api_client, mocker):
    """Staging an already-staged run should 409 without touching the DB."""
    mocker.patch.object(
        DB,
        "get_run",
        return_value={"id": "r1", "staged": True},
    )
    stage_spy = mocker.patch.object(DB, "stage_run")

    resp = await api_client.post("/api/runs/r1/stage")
    assert resp.status_code == 409
    stage_spy.assert_not_called()


async def test_stage_run_success(api_client, mocker):
    """POST stage on a non-staged run calls DB.stage_run and returns staged=True."""
    mocker.patch.object(
        DB,
        "get_run",
        return_value={"id": "r1", "staged": False},
    )
    stage_spy = mocker.patch.object(DB, "stage_run", return_value=None)

    resp = await api_client.post("/api/runs/r1/stage")
    assert resp.status_code == 200
    assert resp.json() == {"run_id": "r1", "staged": True}
    stage_spy.assert_called_once_with("r1")


async def test_commit_run_missing_returns_404(api_client, mocker):
    """POST /api/runs/{run_id}/commit returns 404 when run does not exist."""
    mocker.patch.object(DB, "get_run", return_value=None)
    commit_spy = mocker.patch.object(DB, "commit_staged_run")

    resp = await api_client.post("/api/runs/does-not-exist/commit")
    assert resp.status_code == 404
    commit_spy.assert_not_called()


async def test_commit_run_not_staged_returns_409(api_client, mocker):
    """Committing a run that is not staged should 409."""
    mocker.patch.object(
        DB,
        "get_run",
        return_value={"id": "r1", "staged": False},
    )
    commit_spy = mocker.patch.object(DB, "commit_staged_run")

    resp = await api_client.post("/api/runs/r1/commit")
    assert resp.status_code == 409
    commit_spy.assert_not_called()


async def test_commit_run_success(api_client, mocker):
    """POST commit on a staged run calls DB.commit_staged_run and returns staged=False."""
    mocker.patch.object(
        DB,
        "get_run",
        return_value={"id": "r1", "staged": True},
    )
    commit_spy = mocker.patch.object(DB, "commit_staged_run", return_value=None)

    resp = await api_client.post("/api/runs/r1/commit")
    assert resp.status_code == 200
    assert resp.json() == {"run_id": "r1", "staged": False}
    commit_spy.assert_called_once_with("r1")


async def test_continue_question_missing_returns_404(api_client, mocker):
    """POST /api/questions/{id}/continue returns 404 if question missing."""
    mocker.patch.object(DB, "get_page", return_value=None)
    create_task_spy = mocker.patch("rumil.api.app.asyncio.create_task")

    resp = await api_client.post(
        "/api/questions/not-a-real-id/continue",
        json={"budget": 5},
    )
    assert resp.status_code == 404
    create_task_spy.assert_not_called()


def _fake_question_page(page_id: str = "q1"):
    from rumil.models import Page, PageLayer, PageType, Workspace

    return Page(
        id=page_id,
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="test question",
        headline="test question",
    )


def _fake_claim_page(page_id: str = "c1"):
    from rumil.models import Page, PageLayer, PageType, Workspace

    return Page(
        id=page_id,
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="test claim",
        headline="test claim",
    )


async def test_continue_question_wrong_type_returns_400(api_client, mocker):
    """POST continue on a non-question page returns 400."""
    claim = _fake_claim_page()
    mocker.patch.object(DB, "get_page", return_value=claim)
    create_task_spy = mocker.patch("rumil.api.app.asyncio.create_task")

    resp = await api_client.post(
        f"/api/questions/{claim.id}/continue",
        json={"budget": 5},
    )
    assert resp.status_code == 400
    create_task_spy.assert_not_called()


async def test_continue_question_invalid_budget_returns_400(api_client, mocker):
    """Budget < 1 is rejected."""
    question = _fake_question_page()
    mocker.patch.object(DB, "get_page", return_value=question)
    create_task_spy = mocker.patch("rumil.api.app.asyncio.create_task")

    resp = await api_client.post(
        f"/api/questions/{question.id}/continue",
        json={"budget": 0},
    )
    assert resp.status_code == 400
    create_task_spy.assert_not_called()


async def test_continue_question_success_spawns_task(api_client, mocker):
    """Happy path: returns 202 with a new run_id and schedules a task.

    The background orchestrator itself is not invoked here — we only
    verify the handler's synchronous response and that a task was
    created. This keeps the test from hitting the LLM.
    """
    question = _fake_question_page()
    mocker.patch.object(DB, "get_page", return_value=question)
    create_task_spy = mocker.patch("rumil.api.app.asyncio.create_task")

    resp = await api_client.post(
        f"/api/questions/{question.id}/continue",
        json={"budget": 7},
    )
    assert resp.status_code == 202
    data = resp.json()
    assert data["question_id"] == question.id
    assert data["budget"] == 7
    assert len(data["run_id"]) > 0
    create_task_spy.assert_called_once()


async def test_ab_eval_same_run_returns_400(api_client, mocker):
    """Cannot compare a run against itself."""
    get_run_spy = mocker.patch.object(DB, "get_run")
    create_task_spy = mocker.patch("rumil.api.app.asyncio.create_task")

    resp = await api_client.post(
        "/api/ab-evals",
        json={"run_id_a": "same", "run_id_b": "same"},
    )
    assert resp.status_code == 400
    get_run_spy.assert_not_called()
    create_task_spy.assert_not_called()


async def test_ab_eval_missing_run_a_returns_404(api_client, mocker):
    """POST /api/ab-evals 404s if run_id_a doesn't exist."""
    mocker.patch.object(DB, "get_run", return_value=None)
    create_task_spy = mocker.patch("rumil.api.app.asyncio.create_task")

    resp = await api_client.post(
        "/api/ab-evals",
        json={"run_id_a": "missing", "run_id_b": "r2"},
    )
    assert resp.status_code == 404
    create_task_spy.assert_not_called()


async def test_ab_eval_missing_run_b_returns_404(api_client, mocker):
    """404 if run_id_b doesn't exist (run_id_a does)."""
    calls = []

    async def fake_get_run(self, run_id: str):
        calls.append(run_id)
        return {"id": run_id, "project_id": "proj"} if run_id == "r_a" else None

    mocker.patch.object(DB, "get_run", fake_get_run)
    create_task_spy = mocker.patch("rumil.api.app.asyncio.create_task")

    resp = await api_client.post(
        "/api/ab-evals",
        json={"run_id_a": "r_a", "run_id_b": "r_missing"},
    )
    assert resp.status_code == 404
    assert calls == ["r_a", "r_missing"]
    create_task_spy.assert_not_called()


async def test_ab_eval_success_spawns_task(api_client, mocker):
    """Happy path: 202 and a background task is scheduled."""

    async def fake_get_run(self, run_id: str):
        return {"id": run_id, "project_id": "proj"}

    mocker.patch.object(DB, "get_run", fake_get_run)
    create_task_spy = mocker.patch("rumil.api.app.asyncio.create_task")

    resp = await api_client.post(
        "/api/ab-evals",
        json={"run_id_a": "r_a", "run_id_b": "r_b"},
    )
    assert resp.status_code == 202
    data = resp.json()
    assert data == {"run_id_a": "r_a", "run_id_b": "r_b", "status": "started"}
    create_task_spy.assert_called_once()
