"""Tests for the project-hide PATCH endpoint (Feature 1).

Covers PATCH /api/projects/{id} with ``{hidden: bool}`` — the hide/unhide
affordance for workspaces. Exercised end-to-end against the real local
Supabase so migration/typing drift surfaces here rather than in the UI.
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


async def test_patch_project_hides_and_unhides(api_client, db):
    name = f"test-hide-{uuid.uuid4().hex[:8]}"
    project, _ = await db.get_or_create_project(name)
    try:
        resp = await api_client.patch(f"/api/projects/{project.id}", json={"hidden": True})
        assert resp.status_code == 200
        assert resp.json()["hidden"] is True

        refreshed = await db.get_project(project.id)
        assert refreshed is not None
        assert refreshed.hidden is True

        resp = await api_client.patch(f"/api/projects/{project.id}", json={"hidden": False})
        assert resp.status_code == 200
        assert resp.json()["hidden"] is False
    finally:
        await _delete_project(db, project.id)


async def test_patch_project_missing_returns_404(api_client):
    missing_id = str(uuid.uuid4())
    resp = await api_client.patch(f"/api/projects/{missing_id}", json={"hidden": True})
    assert resp.status_code == 404


async def test_patch_project_empty_body_returns_422(api_client, db):
    name = f"test-empty-{uuid.uuid4().hex[:8]}"
    project, _ = await db.get_or_create_project(name)
    try:
        resp = await api_client.patch(f"/api/projects/{project.id}", json={})
        assert resp.status_code == 422
    finally:
        await _delete_project(db, project.id)


async def test_patch_project_hidden_excluded_from_summary(api_client, db):
    """Once hidden, a project should drop out of /api/projects/summary."""
    name = f"test-summary-{uuid.uuid4().hex[:8]}"
    project, _ = await db.get_or_create_project(name)
    try:
        summary = await api_client.get("/api/projects/summary")
        ids = {p["id"] for p in summary.json()}
        assert project.id in ids

        await api_client.patch(f"/api/projects/{project.id}", json={"hidden": True})

        summary = await api_client.get("/api/projects/summary")
        ids = {p["id"] for p in summary.json()}
        assert project.id not in ids
    finally:
        await _delete_project(db, project.id)
