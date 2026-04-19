"""Tests for POST /api/projects/{project_id}/questions.

The endpoint creates a bare root question — a Page with
``page_type=QUESTION, layer=SQUIDGY, workspace=RESEARCH`` attached to a
project — with no orchestrator dispatch and no call record. It is the
backend half of the Parma landing-page "ask a question" form, so a user
who has just created a workspace can seed their first question before
any research has run.
"""

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from rumil.api.app import app
from rumil.database import DB
from rumil.models import PageLayer, PageType, Workspace


@pytest_asyncio.fixture
async def db():
    d = await DB.create(run_id=str(uuid.uuid4()))
    yield d
    await d.close()


@pytest_asyncio.fixture
async def project(db):
    name = f"test-rq-endpoint-{uuid.uuid4().hex[:8]}"
    project, _ = await db.get_or_create_project(name)
    yield project
    await db._execute(db.client.table("pages").delete().eq("project_id", project.id))
    await db._execute(db.client.table("projects").delete().eq("id", project.id))


@pytest.fixture
def api_client():
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


async def test_post_questions_creates_page_with_expected_type_layer_workspace(
    api_client, db, project
):
    resp = await api_client.post(
        f"/api/projects/{project.id}/questions",
        json={"headline": "Will synthetic biology reshape industrial chemistry by 2035?"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["page_type"] == PageType.QUESTION.value
    assert body["layer"] == PageLayer.SQUIDGY.value
    assert body["workspace"] == Workspace.RESEARCH.value
    assert body["project_id"] == project.id
    assert body["headline"] == "Will synthetic biology reshape industrial chemistry by 2035?"
    assert body["id"]

    db.project_id = project.id
    stored = await db.get_page(body["id"])
    assert stored is not None
    assert stored.page_type == PageType.QUESTION
    assert stored.layer == PageLayer.SQUIDGY
    assert stored.workspace == Workspace.RESEARCH
    assert stored.project_id == project.id


async def test_post_questions_defaults_content_to_headline_when_omitted(api_client, db, project):
    headline = "How quickly do transformer inference costs fall?"
    resp = await api_client.post(
        f"/api/projects/{project.id}/questions",
        json={"headline": headline},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["content"] == headline
    assert body["abstract"] == ""


async def test_post_questions_stores_content_when_provided(api_client, db, project):
    headline = "Will lab-grown meat reach price parity with beef by 2030?"
    content = (
        "Interested in the interaction between fermentation scale-up, "
        "regulatory approval pathways, and consumer acceptance."
    )
    resp = await api_client.post(
        f"/api/projects/{project.id}/questions",
        json={"headline": headline, "content": content},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["headline"] == headline
    assert body["content"] == content
    assert body["abstract"] == content


async def test_post_questions_trims_whitespace_on_headline(api_client, project):
    resp = await api_client.post(
        f"/api/projects/{project.id}/questions",
        json={"headline": "  How does this render?  "},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["headline"] == "How does this render?"


async def test_post_questions_appears_in_root_questions_list(api_client, project):
    resp = await api_client.post(
        f"/api/projects/{project.id}/questions",
        json={"headline": "Is the new endpoint wired into listing?"},
    )
    assert resp.status_code == 200
    new_id = resp.json()["id"]

    list_resp = await api_client.get(f"/api/projects/{project.id}/questions")
    assert list_resp.status_code == 200
    ids = {q["id"] for q in list_resp.json()}
    assert new_id in ids


async def test_post_questions_no_call_record_created(api_client, db, project):
    """Creating a bare question must not create a Call row — that's the whole
    point of this endpoint vs. ``main.py "..."`` which kicks off research."""
    before = await db.client.table("calls").select("id").eq("project_id", project.id).execute()
    before_count = len(before.data or [])

    resp = await api_client.post(
        f"/api/projects/{project.id}/questions",
        json={"headline": "Does creating a question leave the calls table alone?"},
    )
    assert resp.status_code == 200

    after = await db.client.table("calls").select("id").eq("project_id", project.id).execute()
    after_count = len(after.data or [])
    assert after_count == before_count


@pytest.mark.parametrize(
    "payload",
    (
        {"headline": ""},
        {"headline": "   "},
        {"headline": "\t\n"},
    ),
)
async def test_post_questions_rejects_empty_or_whitespace(api_client, project, payload):
    resp = await api_client.post(
        f"/api/projects/{project.id}/questions",
        json=payload,
    )
    assert resp.status_code == 422


async def test_post_questions_rejects_overlong_headline(api_client, project):
    resp = await api_client.post(
        f"/api/projects/{project.id}/questions",
        json={"headline": "x" * 400},
    )
    assert resp.status_code == 422


async def test_post_questions_404_for_unknown_project(api_client):
    resp = await api_client.post(
        f"/api/projects/{uuid.uuid4()}/questions",
        json={"headline": "This project does not exist"},
    )
    assert resp.status_code == 404
