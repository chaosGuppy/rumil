"""Tests for GET /api/projects/{project_id}/search.

The endpoint does a case-insensitive ILIKE across ``pages.headline`` and
``pages.content`` scoped to one project, and returns a ``~200 char``
snippet around the first match so a dropdown can render it inline.
"""

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from rumil.api.app import app
from rumil.database import DB
from rumil.models import Page, PageLayer, PageType, Workspace


@pytest.fixture
def api_client():
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


async def _seed_page(
    db: DB,
    *,
    headline: str,
    content: str,
    page_type: PageType = PageType.CLAIM,
) -> Page:
    page = Page(
        page_type=page_type,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        headline=headline,
        content=content,
    )
    await db.save_page(page)
    return page


@pytest_asyncio.fixture
async def other_project_db(tmp_db):
    """A second DB pointing at a different project, used to prove scoping.

    tmp_db already owns its own project; this fixture mints a sibling.
    Cleaned up at end-of-test by the fixture itself.
    """
    other = await DB.create(run_id=str(uuid.uuid4()))
    project, _ = await other.get_or_create_project(f"test-search-other-{uuid.uuid4().hex[:8]}")
    other.project_id = project.id
    try:
        yield other
    finally:
        await other.delete_run_data(delete_project=True)
        await other.close()


async def test_search_empty_query_returns_empty(api_client, tmp_db):
    resp = await api_client.get(
        f"/api/projects/{tmp_db.project_id}/search",
        params={"q": ""},
    )
    assert resp.status_code == 200
    assert resp.json() == {"results": []}


async def test_search_whitespace_query_returns_empty(api_client, tmp_db):
    resp = await api_client.get(
        f"/api/projects/{tmp_db.project_id}/search",
        params={"q": "   "},
    )
    assert resp.status_code == 200
    assert resp.json() == {"results": []}


async def test_search_matches_headline_case_insensitively(api_client, tmp_db):
    hit = await _seed_page(
        tmp_db,
        headline="Dennard scaling ended around 2006",
        content="Unrelated body text for the hit page.",
    )
    await _seed_page(
        tmp_db,
        headline="Completely different topic",
        content="No match here.",
    )

    resp = await api_client.get(
        f"/api/projects/{tmp_db.project_id}/search",
        params={"q": "DENNARD"},
    )
    assert resp.status_code == 200
    body = resp.json()
    ids = [r["page"]["id"] for r in body["results"]]
    assert hit.id in ids


async def test_search_matches_content_and_returns_snippet(api_client, tmp_db):
    # Long content so we exercise the windowing path. The match word sits
    # in the middle so we expect an ellipsis on both ends.
    body = "intro " * 40 + "The critical claim is that transistor density doubled. " + "tail " * 40
    page = await _seed_page(
        tmp_db,
        headline="Moore",
        content=body,
    )

    resp = await api_client.get(
        f"/api/projects/{tmp_db.project_id}/search",
        params={"q": "transistor density"},
    )
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert len(results) == 1
    assert results[0]["page"]["id"] == page.id
    snippet = results[0]["snippet"]
    assert "transistor density" in snippet.lower()
    assert snippet.startswith("...")
    assert snippet.endswith("...")
    # ~200 chars plus ellipses — keep it well-bounded.
    assert len(snippet) < 250


async def test_search_excludes_superseded_pages(api_client, tmp_db):
    keeper = await _seed_page(
        tmp_db,
        headline="The Fermi paradox keeps recurring",
        content="Active version of the claim.",
    )
    stale = await _seed_page(
        tmp_db,
        headline="The Fermi paradox keeps recurring (old draft)",
        content="Stale draft version.",
    )
    # Supersede `stale` so it should drop out of search results.
    await tmp_db.supersede_page(stale.id, keeper.id)

    resp = await api_client.get(
        f"/api/projects/{tmp_db.project_id}/search",
        params={"q": "Fermi paradox"},
    )
    assert resp.status_code == 200
    ids = {r["page"]["id"] for r in resp.json()["results"]}
    assert keeper.id in ids
    assert stale.id not in ids


async def test_search_scoped_to_project(api_client, tmp_db, other_project_db):
    await _seed_page(
        other_project_db,
        headline="Canal boat on the Rhône",
        content="This page belongs to another project.",
    )
    mine = await _seed_page(
        tmp_db,
        headline="Canal boat in my workspace",
        content="My project's own canal boat page.",
    )

    resp = await api_client.get(
        f"/api/projects/{tmp_db.project_id}/search",
        params={"q": "canal boat"},
    )
    assert resp.status_code == 200
    ids = {r["page"]["id"] for r in resp.json()["results"]}
    assert mine.id in ids
    # Cross-project bleed would be a serious bug — guard against it.
    assert all(r["page"]["id"] != "" for r in resp.json()["results"])
    # Explicit: the other project's page must not leak.
    foreign = await other_project_db._execute(
        other_project_db.client.table("pages")
        .select("id")
        .eq("project_id", other_project_db.project_id)
        .ilike("headline", "%Rhône%")
    )
    foreign_ids = {r["id"] for r in (foreign.data or [])}
    assert foreign_ids.isdisjoint(ids)


async def test_search_respects_limit(api_client, tmp_db):
    for i in range(5):
        await _seed_page(
            tmp_db,
            headline=f"Repeat term NEEDLE-{i}",
            content=f"body {i}",
        )

    resp = await api_client.get(
        f"/api/projects/{tmp_db.project_id}/search",
        params={"q": "NEEDLE", "limit": 3},
    )
    assert resp.status_code == 200
    assert len(resp.json()["results"]) == 3
