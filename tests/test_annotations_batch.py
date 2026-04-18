"""Tests for the batched /api/pages/annotations endpoint.

The endpoint replaces parma's N-parallel per-page fetch with one HTTP
request that issues a single DB query. See
src/rumil/api/app.py::list_pages_annotations_batch.
"""

import uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from rumil.api.app import _ANNOTATIONS_BATCH_MAX, app
from rumil.database import DB
from rumil.models import (
    Page,
    PageLayer,
    PageType,
    Workspace,
)


async def _make_db(project_id: str, staged: bool = False) -> DB:
    db = await DB.create(run_id=str(uuid.uuid4()), staged=staged)
    db.project_id = project_id
    if staged:
        db.snapshot_ts = datetime.max.replace(tzinfo=UTC)
    return db


async def _make_page(db: DB, headline: str = "page") -> Page:
    page = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        headline=headline,
        content=f"content for {headline}",
    )
    await db.save_page(page)
    return page


@pytest_asyncio.fixture
async def project_id():
    db = await DB.create(run_id=str(uuid.uuid4()))
    project = await db.get_or_create_project(f"test-ann-batch-{uuid.uuid4().hex[:8]}")
    yield project.id
    await db._execute(db.client.table("projects").delete().eq("id", project.id))
    await db.close()


@pytest_asyncio.fixture
async def run_db(project_id):
    db = await _make_db(project_id, staged=False)
    await db.create_run(name="test", question_id=None, config={"orchestrator": "two_phase"})
    await db.init_budget(100)
    yield db
    await db.delete_run_data()
    await db.close()


@pytest.fixture
def api_client():
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


async def test_batch_endpoint_returns_grouped_annotations(api_client, run_db):
    p1 = await _make_page(run_db, "p1")
    p2 = await _make_page(run_db, "p2")
    p3 = await _make_page(run_db, "p3")

    await run_db.record_annotation(
        annotation_type="flag",
        author_type="human",
        author_id="u",
        target_page_id=p1.id,
        note="p1-a",
    )
    await run_db.record_annotation(
        annotation_type="flag",
        author_type="human",
        author_id="u",
        target_page_id=p1.id,
        note="p1-b",
    )
    await run_db.record_annotation(
        annotation_type="endorsement",
        author_type="human",
        author_id="u",
        target_page_id=p2.id,
        note="p2-a",
    )
    await run_db.record_annotation(
        annotation_type="span",
        author_type="human",
        author_id="u",
        target_page_id=p3.id,
        span_start=0,
        span_end=3,
        note="p3-a",
    )
    await run_db.record_annotation(
        annotation_type="span",
        author_type="human",
        author_id="u",
        target_page_id=p3.id,
        span_start=5,
        span_end=7,
        note="p3-b",
    )

    ids = f"{p1.id},{p2.id},{p3.id}"
    resp = await api_client.get(f"/api/pages/annotations?ids={ids}")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {p1.id, p2.id, p3.id}
    assert len(body[p1.id]) == 2
    assert len(body[p2.id]) == 1
    assert len(body[p3.id]) == 2
    assert {a["note"] for a in body[p1.id]} == {"p1-a", "p1-b"}
    assert body[p2.id][0]["annotation_type"] == "endorsement"
    assert {a["span_start"] for a in body[p3.id]} == {0, 5}


async def test_batch_endpoint_empty_ids_returns_empty_map(api_client, run_db):
    resp = await api_client.get("/api/pages/annotations?ids=")
    assert resp.status_code == 200
    assert resp.json() == {}


async def test_batch_endpoint_unknown_pages_included_as_empty(api_client, run_db):
    p1 = await _make_page(run_db, "p1")
    unknown = str(uuid.uuid4())

    await run_db.record_annotation(
        annotation_type="flag",
        author_type="human",
        author_id="u",
        target_page_id=p1.id,
        note="known",
    )

    resp = await api_client.get(f"/api/pages/annotations?ids={p1.id},{unknown}")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {p1.id, unknown}
    assert len(body[p1.id]) == 1
    assert body[unknown] == []


async def test_batch_endpoint_rejects_too_many_ids(api_client, run_db):
    ids = ",".join(str(uuid.uuid4()) for _ in range(_ANNOTATIONS_BATCH_MAX + 1))
    resp = await api_client.get(f"/api/pages/annotations?ids={ids}")
    assert resp.status_code == 400


async def test_batch_endpoint_respects_staging(project_id):
    baseline_db = await _make_db(project_id, staged=False)
    await baseline_db.create_run(name="baseline", question_id=None)
    await baseline_db.init_budget(100)

    staged_db = await _make_db(project_id, staged=True)
    await staged_db.create_run(name="staged", question_id=None)
    await staged_db.init_budget(100)

    try:
        page_baseline = await _make_page(baseline_db, "baseline-page")
        page_staged = await _make_page(staged_db, "staged-page")

        await baseline_db.record_annotation(
            annotation_type="flag",
            author_type="human",
            author_id="baseline",
            target_page_id=page_baseline.id,
            note="b1",
        )
        await staged_db.record_annotation(
            annotation_type="flag",
            author_type="human",
            author_id="staged",
            target_page_id=page_staged.id,
            note="s1",
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            ids = f"{page_baseline.id},{page_staged.id}"

            resp = await client.get(f"/api/pages/annotations?ids={ids}")
            assert resp.status_code == 200
            body = resp.json()
            assert len(body[page_baseline.id]) == 1
            assert body[page_staged.id] == []

            resp = await client.get(
                f"/api/pages/annotations?ids={ids}&staged_run_id={staged_db.run_id}"
            )
            assert resp.status_code == 200
            body = resp.json()
            assert len(body[page_baseline.id]) == 1
            assert len(body[page_staged.id]) == 1
            assert body[page_staged.id][0]["note"] == "s1"
    finally:
        await baseline_db.delete_run_data()
        await staged_db.delete_run_data()
        await baseline_db.close()
        await staged_db.close()


async def test_legacy_per_page_endpoint_still_works(api_client, run_db):
    """Regression: the old GET /api/pages/{id}/annotations still works after
    the additive batch endpoint was introduced.
    """
    p = await _make_page(run_db, "solo")
    await run_db.record_annotation(
        annotation_type="endorsement",
        author_type="human",
        author_id="u",
        target_page_id=p.id,
        note="legacy",
    )
    resp = await api_client.get(f"/api/pages/{p.id}/annotations")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["note"] == "legacy"
