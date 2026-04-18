"""Tests for the POST /api/view-items/{item_id}/flag endpoint.

Covers the friendly-user view-reading surface: flagging a view item
writes a row to page_flags with flag_type='view_item_issue' and records
a reputation event. Gated by settings.enable_flag_issue.
"""

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from rumil.api.app import app
from rumil.models import Page, PageLayer, PageType, Workspace
from rumil.settings import override_settings


@pytest.fixture
def api_client():
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


@pytest_asyncio.fixture(autouse=True)
async def _cleanup_endpoint_runs(tmp_db):
    """The flag endpoint creates its own 'friendly-user-flag' runs tied to
    tmp_db.project_id. Clean them up before tmp_db tries to delete the
    project so the FK constraint holds.
    """
    yield
    if not tmp_db.project_id:
        return
    rows = (
        await tmp_db._execute(
            tmp_db.client.table("runs").select("id").eq("project_id", tmp_db.project_id)
        )
    ).data
    for row in rows:
        if row["id"] == tmp_db.run_id:
            continue
        await tmp_db._execute(
            tmp_db.client.table("reputation_events").delete().eq("run_id", row["id"])
        )
        await tmp_db._execute(tmp_db.client.table("page_flags").delete().eq("run_id", row["id"]))
        await tmp_db._execute(tmp_db.client.table("runs").delete().eq("id", row["id"]))


async def _make_claim_page(tmp_db):
    page = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="Sky appears blue due to Rayleigh scattering.",
        headline="Rayleigh scattering",
        credence=7,
        robustness=4,
        importance=1,
    )
    await tmp_db.save_page(page)
    return page


async def _read_flags_for_page(db, page_id):
    res = await db._execute(db.client.table("page_flags").select("*").eq("page_id", page_id))
    return res.data


async def test_flag_view_item_writes_page_flag_row(api_client, tmp_db):
    page = await _make_claim_page(tmp_db)

    with override_settings(enable_flag_issue=True):
        resp = await api_client.post(
            f"/api/view-items/{page.id}/flag",
            json={
                "category": "problem",
                "message": "The credence seems too high given only one source.",
            },
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["page_id"] == page.id

    rows = await _read_flags_for_page(tmp_db, page.id)
    assert len(rows) == 1
    row = rows[0]
    assert row["flag_type"] == "view_item_issue"
    assert row["page_id"] == page.id
    assert row["call_id"] is None
    assert row["note"] == "[problem] The credence seems too high given only one source."


async def test_flag_view_item_appends_suggested_fix(api_client, tmp_db):
    page = await _make_claim_page(tmp_db)

    with override_settings(enable_flag_issue=True):
        resp = await api_client.post(
            f"/api/view-items/{page.id}/flag",
            json={
                "category": "improvement",
                "message": "Claim is missing a citation.",
                "suggested_fix": "Add the optics textbook reference.",
            },
        )

    assert resp.status_code == 200
    rows = await _read_flags_for_page(tmp_db, page.id)
    assert len(rows) == 1
    note = rows[0]["note"]
    assert note.startswith("[improvement] Claim is missing a citation.")
    assert "\n\nSuggested fix: Add the optics textbook reference." in note


async def test_flag_view_item_returns_403_when_disabled(api_client, tmp_db):
    page = await _make_claim_page(tmp_db)

    with override_settings(enable_flag_issue=False):
        resp = await api_client.post(
            f"/api/view-items/{page.id}/flag",
            json={"category": "problem", "message": "x"},
        )

    assert resp.status_code == 403

    rows = await _read_flags_for_page(tmp_db, page.id)
    assert rows == []


async def test_flag_view_item_returns_404_for_unknown_page(api_client, tmp_db):
    with override_settings(enable_flag_issue=True):
        resp = await api_client.post(
            "/api/view-items/00000000-0000-0000-0000-000000000000/flag",
            json={"category": "problem", "message": "x"},
        )

    assert resp.status_code == 404


async def test_flag_view_item_rejects_invalid_category(api_client, tmp_db):
    page = await _make_claim_page(tmp_db)

    with override_settings(enable_flag_issue=True):
        resp = await api_client.post(
            f"/api/view-items/{page.id}/flag",
            json={"category": "garbage", "message": "x"},
        )

    assert resp.status_code == 422

    rows = await _read_flags_for_page(tmp_db, page.id)
    assert rows == []


async def test_flag_view_item_resolves_short_id(api_client, tmp_db):
    page = await _make_claim_page(tmp_db)
    short_id = page.id[:8]

    with override_settings(enable_flag_issue=True):
        resp = await api_client.post(
            f"/api/view-items/{short_id}/flag",
            json={"category": "problem", "message": "short id test"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["page_id"] == page.id

    rows = await _read_flags_for_page(tmp_db, page.id)
    assert len(rows) == 1
    assert rows[0]["note"] == "[problem] short id test"


async def test_flag_view_item_records_reputation_event(api_client, tmp_db):
    page = await _make_claim_page(tmp_db)

    with override_settings(enable_flag_issue=True):
        resp = await api_client.post(
            f"/api/view-items/{page.id}/flag",
            json={"category": "problem", "message": "credence looks wrong"},
        )

    assert resp.status_code == 200

    res = await tmp_db._execute(
        tmp_db.client.table("reputation_events")
        .select("*")
        .eq("project_id", tmp_db.project_id)
        .eq("source", "human_feedback")
        .eq("dimension", "view_item_issue")
    )
    events = res.data
    assert len(events) == 1
    assert events[0]["score"] == 1.0
    extra = events[0]["extra"]
    assert extra["flagged_page_id"] == page.id
    assert extra["category"] == "problem"


async def test_get_app_config_exposes_enable_flag_issue(api_client):
    with override_settings(enable_flag_issue=True):
        resp = await api_client.get("/api/config")
    assert resp.status_code == 200
    assert resp.json() == {"enable_flag_issue": True}

    with override_settings(enable_flag_issue=False):
        resp = await api_client.get("/api/config")
    assert resp.status_code == 200
    assert resp.json() == {"enable_flag_issue": False}
