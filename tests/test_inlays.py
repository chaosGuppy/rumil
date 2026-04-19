"""Tests for the Inlay feature — INLAY pages and INLAY_OF links.

See planning/inlay-ui.md. The MVP surface is:
  - PageType.INLAY rows save/load round-trip via db.save_page / db.get_page.
  - db.get_inlays_for_question returns active inlays for a question and
    excludes superseded ones.
  - GET /api/questions/{qid}/inlays is a thin wrapper around the above.
"""

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from rumil.api.app import app
from rumil.database import DB
from rumil.models import (
    LinkType,
    Page,
    PageLayer,
    PageLink,
    PageType,
    Workspace,
)


@pytest_asyncio.fixture
async def db():
    d = await DB.create(run_id=str(uuid.uuid4()))
    yield d
    await d.close()


@pytest_asyncio.fixture
async def project(db):
    name = f"test-inlay-{uuid.uuid4().hex[:8]}"
    project, _ = await db.get_or_create_project(name)
    db.project_id = project.id
    yield project
    await db._execute(
        db.client.table("page_links").delete().eq("run_id", db.run_id),
    )
    await db._execute(db.client.table("pages").delete().eq("project_id", project.id))
    await db._execute(db.client.table("projects").delete().eq("id", project.id))


@pytest_asyncio.fixture
async def question(db, project):
    page = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="Test question body.",
        headline="Is the inlay pipeline working?",
        project_id=project.id,
    )
    await db.save_page(page)
    return page


@pytest.fixture
def api_client():
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


async def _save_inlay(
    db: DB,
    *,
    project_id: str,
    target_id: str,
    headline: str,
    content: str = "<!doctype html><html><body>hi</body></html>",
) -> Page:
    inlay = Page(
        page_type=PageType.INLAY,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=content,
        headline=headline,
        project_id=project_id,
        provenance_model="human",
        extra={
            "target_id": target_id,
            "author_kind": "user",
            "api_version": "rumil.inlay.v1",
        },
    )
    await db.save_page(inlay)
    await db.save_link(
        PageLink(
            from_page_id=inlay.id,
            to_page_id=target_id,
            link_type=LinkType.INLAY_OF,
        ),
    )
    return inlay


async def test_inlay_page_round_trips_through_save_and_get(db, project, question):
    inlay = await _save_inlay(
        db,
        project_id=project.id,
        target_id=question.id,
        headline="Forecast card",
        content="<!doctype html><html><body>forecast</body></html>",
    )
    stored = await db.get_page(inlay.id)
    assert stored is not None
    assert stored.page_type == PageType.INLAY
    assert stored.layer == PageLayer.SQUIDGY
    assert stored.workspace == Workspace.RESEARCH
    assert stored.project_id == project.id
    assert stored.content == "<!doctype html><html><body>forecast</body></html>"
    assert stored.extra["target_id"] == question.id
    assert stored.extra["author_kind"] == "user"
    assert stored.extra["api_version"] == "rumil.inlay.v1"


async def test_get_inlays_for_question_returns_linked_inlays(db, project, question):
    inlay_a = await _save_inlay(
        db,
        project_id=project.id,
        target_id=question.id,
        headline="Inlay A",
    )
    inlay_b = await _save_inlay(
        db,
        project_id=project.id,
        target_id=question.id,
        headline="Inlay B",
    )

    inlays = await db.get_inlays_for_question(question.id)
    ids = {p.id for p in inlays}
    assert inlay_a.id in ids
    assert inlay_b.id in ids
    for p in inlays:
        assert p.page_type == PageType.INLAY


async def test_get_inlays_for_question_excludes_superseded(db, project, question):
    active = await _save_inlay(
        db,
        project_id=project.id,
        target_id=question.id,
        headline="Active inlay",
    )
    superseded = await _save_inlay(
        db,
        project_id=project.id,
        target_id=question.id,
        headline="Old inlay",
    )
    await db._execute(
        db.client.table("pages")
        .update({"is_superseded": True, "superseded_by": active.id})
        .eq("id", superseded.id),
    )

    inlays = await db.get_inlays_for_question(question.id)
    ids = {p.id for p in inlays}
    assert active.id in ids
    assert superseded.id not in ids


async def test_get_inlays_for_question_empty_when_no_inlays(db, project, question):
    inlays = await db.get_inlays_for_question(question.id)
    assert inlays == []


async def test_inlays_endpoint_returns_inlay_pages(api_client, db, project, question):
    inlay = await _save_inlay(
        db,
        project_id=project.id,
        target_id=question.id,
        headline="Endpoint inlay",
    )
    resp = await api_client.get(f"/api/questions/{question.id}/inlays")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    ids = [row["id"] for row in body]
    assert inlay.id in ids
    match = next(row for row in body if row["id"] == inlay.id)
    assert match["page_type"] == PageType.INLAY.value
    assert match["headline"] == "Endpoint inlay"


async def test_inlays_endpoint_returns_empty_list_when_no_inlays(api_client, question):
    resp = await api_client.get(f"/api/questions/{question.id}/inlays")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_inlays_endpoint_rejects_non_question_target(api_client, db, project, question):
    claim = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="Not a question.",
        headline="Some claim",
        project_id=project.id,
    )
    await db.save_page(claim)
    resp = await api_client.get(f"/api/questions/{claim.id}/inlays")
    assert resp.status_code == 404


async def test_inlays_endpoint_404_for_unknown_question(api_client):
    resp = await api_client.get(f"/api/questions/{uuid.uuid4()}/inlays")
    assert resp.status_code == 404


async def test_inlays_endpoint_accepts_short_id(api_client, db, project, question):
    inlay = await _save_inlay(
        db,
        project_id=project.id,
        target_id=question.id,
        headline="Short ID lookup",
    )
    short = question.id[:8]
    resp = await api_client.get(f"/api/questions/{short}/inlays")
    assert resp.status_code == 200
    ids = [row["id"] for row in resp.json()]
    assert inlay.id in ids
