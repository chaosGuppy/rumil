"""Tests for the run-hide PATCH endpoint (Feature 2).

Covers PATCH /api/runs/{run_id} with ``{hidden: bool}`` — the hide/unhide
affordance for runs in the parma RunPicker. Exercises the endpoint
end-to-end against local Supabase.
"""

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from rumil.api.app import app
from rumil.database import DB


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
    await db._execute(db.client.table("runs").delete().eq("id", run_id))


async def test_patch_run_hides_and_unhides(api_client, db):
    name = f"test-run-hide-{uuid.uuid4().hex[:8]}"
    project, _ = await db.get_or_create_project(name)
    run_id = str(uuid.uuid4())
    db.project_id = project.id
    db.run_id = run_id
    await db.create_run(name="test-run", question_id=None)
    try:
        resp = await api_client.patch(f"/api/runs/{run_id}", json={"hidden": True})
        assert resp.status_code == 200
        body = resp.json()
        assert body["hidden"] is True
        assert body["run_id"] == run_id

        rows = await db.list_runs_for_project(project.id)
        row = next(r for r in rows if r["run_id"] == run_id)
        assert row["hidden"] is True

        resp = await api_client.patch(f"/api/runs/{run_id}", json={"hidden": False})
        assert resp.status_code == 200
        assert resp.json()["hidden"] is False
    finally:
        await _delete_run(db, run_id)
        await _delete_project(db, project.id)


async def test_patch_run_missing_returns_404(api_client):
    missing_id = str(uuid.uuid4())
    resp = await api_client.patch(f"/api/runs/{missing_id}", json={"hidden": True})
    assert resp.status_code == 404


async def test_patch_run_empty_body_returns_422(api_client, db):
    name = f"test-run-empty-{uuid.uuid4().hex[:8]}"
    project, _ = await db.get_or_create_project(name)
    run_id = str(uuid.uuid4())
    db.project_id = project.id
    db.run_id = run_id
    await db.create_run(name="test-run", question_id=None)
    try:
        resp = await api_client.patch(f"/api/runs/{run_id}", json={})
        assert resp.status_code == 422
    finally:
        await _delete_run(db, run_id)
        await _delete_project(db, project.id)
