"""Tests for DB.get_or_create_project and POST /api/projects.

The DB method returns ``(project, created)`` so the HTTP layer can tell
"you just made a new workspace" from "you reused an existing one" — that
distinction drives a subtle hint in the Parma landing modal when a user
types a name that collides with an existing workspace.
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


async def test_get_or_create_project_created_true_for_new_name(db):
    name = f"test-create-{uuid.uuid4().hex[:8]}"
    project, created = await db.get_or_create_project(name)
    try:
        assert created is True
        assert project.name == name
        assert project.id
    finally:
        await _delete_project(db, project.id)


async def test_get_or_create_project_created_false_for_existing_name(db):
    name = f"test-create-{uuid.uuid4().hex[:8]}"
    first, first_created = await db.get_or_create_project(name)
    try:
        assert first_created is True

        second, second_created = await db.get_or_create_project(name)
        assert second_created is False
        assert second.id == first.id
        assert second.name == first.name
    finally:
        await _delete_project(db, first.id)


async def test_post_projects_creates_new_workspace(api_client, db):
    name = f"test-endpoint-{uuid.uuid4().hex[:8]}"
    resp = await api_client.post("/api/projects", json={"name": name})
    assert resp.status_code == 200
    body = resp.json()
    assert body["created"] is True
    assert body["project"]["name"] == name
    assert body["project"]["id"]
    await _delete_project(db, body["project"]["id"])


async def test_post_projects_returns_existing_when_name_collides(api_client, db):
    name = f"test-endpoint-{uuid.uuid4().hex[:8]}"
    first = await api_client.post("/api/projects", json={"name": name})
    assert first.status_code == 200
    first_body = first.json()
    assert first_body["created"] is True
    project_id = first_body["project"]["id"]

    try:
        second = await api_client.post("/api/projects", json={"name": name})
        assert second.status_code == 200
        second_body = second.json()
        assert second_body["created"] is False
        assert second_body["project"]["id"] == project_id
    finally:
        await _delete_project(db, project_id)


async def test_post_projects_trims_whitespace(api_client, db):
    raw = f"test-endpoint-{uuid.uuid4().hex[:8]}"
    resp = await api_client.post("/api/projects", json={"name": f"  {raw}  "})
    assert resp.status_code == 200
    body = resp.json()
    assert body["project"]["name"] == raw
    await _delete_project(db, body["project"]["id"])


@pytest.mark.parametrize(
    "payload",
    (
        {"name": ""},
        {"name": "   "},
        {"name": "\t\n"},
    ),
)
async def test_post_projects_rejects_empty_or_whitespace(api_client, payload):
    resp = await api_client.post("/api/projects", json=payload)
    assert resp.status_code == 422


async def test_post_projects_rejects_overlong_name(api_client):
    resp = await api_client.post("/api/projects", json={"name": "x" * 200})
    assert resp.status_code == 422
