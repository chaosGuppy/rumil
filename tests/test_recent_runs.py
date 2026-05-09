"""Tests for cross-project recent-runs listing (DB + API)."""

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from rumil.api.app import app, require_admin
from rumil.api.auth import AuthUser, get_current_user
from rumil.database import DB
from rumil.models import Page, PageLayer, PageType, Workspace


async def _project_name(db: DB, project_id: str) -> str:
    rows = (await db._execute(db.client.table("projects").select("name").eq("id", project_id))).data
    return rows[0]["name"]


@pytest_asyncio.fixture
async def multi_project_runs(tmp_db):
    project_a_id = tmp_db.project_id

    q_a = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="Project-A question",
        headline="Project-A headline",
    )
    await tmp_db.save_page(q_a)
    await tmp_db.create_run(name="run-a1", question_id=q_a.id)

    run_a2_id = str(uuid.uuid4())
    db_a2 = await DB.create(run_id=run_a2_id)
    db_a2.project_id = project_a_id
    await db_a2.create_run(name="run-a2-no-question", question_id=None)

    project_b = await tmp_db.get_or_create_project(f"test-b-{uuid.uuid4().hex[:8]}")
    run_b_id = str(uuid.uuid4())
    db_b = await DB.create(run_id=run_b_id)
    db_b.project_id = project_b.id
    q_b = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="Project-B question",
        headline="Project-B headline",
    )
    await db_b.save_page(q_b)
    await db_b.create_run(name="run-b1", question_id=q_b.id)

    info = {
        "project_a_id": project_a_id,
        "project_a_name": await _project_name(tmp_db, project_a_id),
        "project_b_id": project_b.id,
        "project_b_name": project_b.name,
        "run_a1_id": tmp_db.run_id,
        "run_a2_id": run_a2_id,
        "run_b_id": run_b_id,
        "q_a_headline": q_a.headline,
        "q_b_headline": q_b.headline,
    }
    try:
        yield info
    finally:
        await db_a2.delete_run_data()
        await db_a2.close()
        await db_b.delete_run_data(delete_project=True)
        await db_b.close()


async def test_list_recent_runs_cross_project(tmp_db, multi_project_runs):
    rows, total = await tmp_db.list_recent_runs(offset=0, limit=50)

    by_id = {r["run_id"]: r for r in rows}
    assert multi_project_runs["run_a1_id"] in by_id
    assert multi_project_runs["run_a2_id"] in by_id
    assert multi_project_runs["run_b_id"] in by_id
    assert total >= 3

    a1 = by_id[multi_project_runs["run_a1_id"]]
    assert a1["project_id"] == multi_project_runs["project_a_id"]
    assert a1["project_name"] == multi_project_runs["project_a_name"]
    assert a1["question_summary"] == multi_project_runs["q_a_headline"]
    assert a1["staged"] is False
    assert a1["name"] == "run-a1"

    b1 = by_id[multi_project_runs["run_b_id"]]
    assert b1["project_id"] == multi_project_runs["project_b_id"]
    assert b1["project_name"] == multi_project_runs["project_b_name"]
    assert b1["question_summary"] == multi_project_runs["q_b_headline"]


async def test_list_recent_runs_null_question(tmp_db, multi_project_runs):
    rows, _ = await tmp_db.list_recent_runs(offset=0, limit=50)
    a2 = next(r for r in rows if r["run_id"] == multi_project_runs["run_a2_id"])
    assert a2["question_summary"] is None
    assert a2["project_name"] == multi_project_runs["project_a_name"]


async def test_list_recent_runs_ordering(tmp_db, multi_project_runs):
    rows, _ = await tmp_db.list_recent_runs(offset=0, limit=50)
    our_ids = {
        multi_project_runs["run_a1_id"],
        multi_project_runs["run_a2_id"],
        multi_project_runs["run_b_id"],
    }
    ours = [r for r in rows if r["run_id"] in our_ids]
    timestamps = [r["created_at"] for r in ours]
    assert timestamps == sorted(timestamps, reverse=True)
    assert ours[0]["run_id"] == multi_project_runs["run_b_id"]


async def test_list_recent_runs_pagination(tmp_db, multi_project_runs):
    rows_first, total_first = await tmp_db.list_recent_runs(offset=0, limit=2)
    rows_second, total_second = await tmp_db.list_recent_runs(offset=2, limit=2)

    assert len(rows_first) == 2
    assert total_first == total_second
    assert total_first >= 3

    first_ids = {r["run_id"] for r in rows_first}
    second_ids = {r["run_id"] for r in rows_second}
    assert first_ids.isdisjoint(second_ids)


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


@pytest.fixture
def api_client_no_admin():
    user = AuthUser(user_id="", email="test@example.com")
    app.dependency_overrides[get_current_user] = lambda: user

    async def _deny():
        from fastapi import HTTPException

        raise HTTPException(status_code=403, detail="admin required")

    app.dependency_overrides[require_admin] = _deny
    transport = ASGITransport(app=app)
    client = AsyncClient(transport=transport, base_url="http://test")
    try:
        yield client
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(require_admin, None)


async def test_admin_runs_endpoint_shape(api_client, multi_project_runs):
    resp = await api_client.get("/api/admin/runs?offset=0&limit=20")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) >= {"items", "total_count", "offset", "limit"}
    assert body["offset"] == 0
    assert body["limit"] == 20
    assert isinstance(body["items"], list)

    ids = {item["run_id"] for item in body["items"]}
    assert multi_project_runs["run_a1_id"] in ids or body["total_count"] > 20

    matching = [item for item in body["items"] if item["run_id"] == multi_project_runs["run_b_id"]]
    if matching:
        b = matching[0]
        assert b["project_id"] == multi_project_runs["project_b_id"]
        assert b["project_name"] == multi_project_runs["project_b_name"]
        assert b["question_summary"] == multi_project_runs["q_b_headline"]


async def test_admin_runs_endpoint_pagination(api_client, multi_project_runs):
    resp = await api_client.get("/api/admin/runs?offset=0&limit=1")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 1
    assert body["limit"] == 1
    assert body["total_count"] >= 3


async def test_admin_runs_endpoint_requires_admin(api_client_no_admin):
    resp = await api_client_no_admin.get("/api/admin/runs")
    assert resp.status_code == 403
