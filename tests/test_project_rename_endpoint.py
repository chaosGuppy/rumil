"""Tests for the project-rename PATCH endpoint (Feature 3).

Covers PATCH /api/projects/{id} with ``{name: str}`` — the inline-rename
affordance for workspaces. Also exercises trim/length/empty/collision
edge cases so the frontend inline-edit UX has consistent server behavior
to rely on.
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


async def test_patch_project_renames(api_client, db):
    original = f"test-rename-{uuid.uuid4().hex[:8]}"
    renamed = f"test-renamed-{uuid.uuid4().hex[:8]}"
    project, _ = await db.get_or_create_project(original)
    try:
        resp = await api_client.patch(f"/api/projects/{project.id}", json={"name": renamed})
        assert resp.status_code == 200
        assert resp.json()["name"] == renamed

        refreshed = await db.get_project(project.id)
        assert refreshed is not None
        assert refreshed.name == renamed
    finally:
        await _delete_project(db, project.id)


async def test_patch_project_rename_trims_whitespace(api_client, db):
    original = f"test-trim-{uuid.uuid4().hex[:8]}"
    target = f"test-trimmed-{uuid.uuid4().hex[:8]}"
    project, _ = await db.get_or_create_project(original)
    try:
        resp = await api_client.patch(f"/api/projects/{project.id}", json={"name": f"  {target}  "})
        assert resp.status_code == 200
        assert resp.json()["name"] == target
    finally:
        await _delete_project(db, project.id)


async def test_patch_project_rename_collision_returns_409(api_client, db):
    a_name = f"test-collide-a-{uuid.uuid4().hex[:8]}"
    b_name = f"test-collide-b-{uuid.uuid4().hex[:8]}"
    project_a, _ = await db.get_or_create_project(a_name)
    project_b, _ = await db.get_or_create_project(b_name)
    try:
        resp = await api_client.patch(f"/api/projects/{project_b.id}", json={"name": a_name})
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert a_name in detail

        refreshed_b = await db.get_project(project_b.id)
        assert refreshed_b is not None
        assert refreshed_b.name == b_name
    finally:
        await _delete_project(db, project_a.id)
        await _delete_project(db, project_b.id)


async def test_patch_project_rename_to_same_name_is_noop(api_client, db):
    """Renaming a project to its existing name should succeed, not 409."""
    name = f"test-same-{uuid.uuid4().hex[:8]}"
    project, _ = await db.get_or_create_project(name)
    try:
        resp = await api_client.patch(f"/api/projects/{project.id}", json={"name": name})
        assert resp.status_code == 200
        assert resp.json()["name"] == name
    finally:
        await _delete_project(db, project.id)


@pytest.mark.parametrize(
    "payload",
    (
        {"name": ""},
        {"name": "   "},
        {"name": "\t\n"},
    ),
)
async def test_patch_project_rejects_empty_or_whitespace_name(api_client, db, payload):
    name = f"test-reject-{uuid.uuid4().hex[:8]}"
    project, _ = await db.get_or_create_project(name)
    try:
        resp = await api_client.patch(f"/api/projects/{project.id}", json=payload)
        assert resp.status_code == 422
    finally:
        await _delete_project(db, project.id)


async def test_patch_project_rejects_overlong_name(api_client, db):
    name = f"test-long-{uuid.uuid4().hex[:8]}"
    project, _ = await db.get_or_create_project(name)
    try:
        resp = await api_client.patch(f"/api/projects/{project.id}", json={"name": "x" * 200})
        assert resp.status_code == 422
    finally:
        await _delete_project(db, project.id)
