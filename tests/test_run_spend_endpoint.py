"""Tests for the GET /api/runs/{run_id}/spend endpoint.

The endpoint aggregates ``cost_usd`` and ``completed_at - created_at`` across
a run's calls, broken down by ``call_type``. These tests seed calls directly
(no LLM) and assert on the aggregated shape — the happy path, the not-found
case, and GROUP BY correctness (multiple calls of the same type collapse
into one row with count > 1).
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from rumil.api.app import app
from rumil.database import DB
from rumil.models import (
    Call,
    CallStatus,
    CallType,
    Workspace,
)


@pytest_asyncio.fixture
async def db():
    d = await DB.create(run_id=str(uuid.uuid4()))
    yield d
    await d.close()


@pytest.fixture
def api_client():
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


async def _delete_project(db: DB, project_id: str) -> None:
    await db._execute(db.client.table("projects").delete().eq("id", project_id))


async def _delete_run(db: DB, run_id: str) -> None:
    await db._execute(db.client.table("calls").delete().eq("run_id", run_id))
    await db._execute(db.client.table("runs").delete().eq("id", run_id))


async def _seed_call(
    db: DB,
    *,
    call_type: CallType,
    cost_usd: float | None,
    duration_ms: int | None,
) -> Call:
    created_at = datetime.now(UTC)
    completed_at = (
        created_at + timedelta(milliseconds=duration_ms) if duration_ms is not None else None
    )
    call = Call(
        call_type=call_type,
        workspace=Workspace.RESEARCH,
        status=CallStatus.COMPLETE if completed_at else CallStatus.PENDING,
        created_at=created_at,
        completed_at=completed_at,
        cost_usd=cost_usd,
    )
    await db.save_call(call)
    return call


@pytest_asyncio.fixture
async def run_with_calls(db):
    name = f"test-spend-{uuid.uuid4().hex[:8]}"
    project, _ = await db.get_or_create_project(name)
    run_id = str(uuid.uuid4())
    db.project_id = project.id
    db.run_id = run_id
    await db.create_run(name="test-spend", question_id=None)

    # 3 assess calls, 1 find_considerations, 1 with no cost/duration.
    await _seed_call(db, call_type=CallType.ASSESS, cost_usd=0.10, duration_ms=3000)
    await _seed_call(db, call_type=CallType.ASSESS, cost_usd=0.20, duration_ms=5000)
    await _seed_call(db, call_type=CallType.ASSESS, cost_usd=0.05, duration_ms=2000)
    await _seed_call(db, call_type=CallType.FIND_CONSIDERATIONS, cost_usd=0.50, duration_ms=10000)
    await _seed_call(db, call_type=CallType.INGEST, cost_usd=None, duration_ms=None)

    yield run_id, project.id

    await _delete_run(db, run_id)
    await _delete_project(db, project.id)


async def test_spend_happy_path(api_client, run_with_calls):
    run_id, _ = run_with_calls
    resp = await api_client.get(f"/api/runs/{run_id}/spend")
    assert resp.status_code == 200
    body = resp.json()

    assert body["run_id"] == run_id
    assert body["run_id_short"] == run_id[:8]
    assert body["total_calls"] == 5
    assert body["total_cost_usd"] == pytest.approx(0.85, abs=1e-6)
    assert body["total_duration_ms"] == 20000

    # Sorted by cost_usd desc: find_considerations (0.50) > assess (0.35) > ingest_source (0.00).
    types_in_order = [row["call_type"] for row in body["by_call_type"]]
    assert types_in_order == [
        CallType.FIND_CONSIDERATIONS.value,
        CallType.ASSESS.value,
        CallType.INGEST.value,
    ]


async def test_spend_group_by_aggregates_by_call_type(api_client, run_with_calls):
    run_id, _ = run_with_calls
    resp = await api_client.get(f"/api/runs/{run_id}/spend")
    assert resp.status_code == 200
    body = resp.json()

    by_type = {row["call_type"]: row for row in body["by_call_type"]}

    assert by_type[CallType.ASSESS.value]["count"] == 3
    assert by_type[CallType.ASSESS.value]["cost_usd"] == pytest.approx(0.35, abs=1e-6)
    assert by_type[CallType.ASSESS.value]["duration_ms"] == 10000

    assert by_type[CallType.FIND_CONSIDERATIONS.value]["count"] == 1
    assert by_type[CallType.FIND_CONSIDERATIONS.value]["cost_usd"] == pytest.approx(0.50, abs=1e-6)
    assert by_type[CallType.FIND_CONSIDERATIONS.value]["duration_ms"] == 10000

    assert by_type[CallType.INGEST.value]["count"] == 1
    assert by_type[CallType.INGEST.value]["cost_usd"] == 0.0
    assert by_type[CallType.INGEST.value]["duration_ms"] == 0


async def test_spend_unknown_run_returns_404(api_client):
    missing_id = str(uuid.uuid4())
    resp = await api_client.get(f"/api/runs/{missing_id}/spend")
    assert resp.status_code == 404


async def test_spend_run_with_no_calls(api_client, db):
    name = f"test-spend-empty-{uuid.uuid4().hex[:8]}"
    project, _ = await db.get_or_create_project(name)
    run_id = str(uuid.uuid4())
    db.project_id = project.id
    db.run_id = run_id
    await db.create_run(name="test-spend-empty", question_id=None)
    try:
        resp = await api_client.get(f"/api/runs/{run_id}/spend")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_calls"] == 0
        assert body["total_cost_usd"] == 0.0
        assert body["total_duration_ms"] == 0
        assert body["by_call_type"] == []
    finally:
        await _delete_run(db, run_id)
        await _delete_project(db, project.id)
