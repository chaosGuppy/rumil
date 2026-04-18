"""Tests for the friendly-user view-reading API surface.

Covers:
- POST /api/view-items/{item_id}/flag      -- flag a view item
- DELETE /api/view-items/flags/{flag_id}   -- undo a just-submitted flag
- POST /api/view-items/{item_id}/read      -- record a read-dwell event
- GET /api/config                          -- feature-flag exposure
- BasicAuthMiddleware two-tier auth        -- friendly vs admin password
"""

import base64
import uuid as _uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from rumil.api import app as app_module
from rumil.api.app import app
from rumil.models import Page, PageLayer, PageType, Workspace
from rumil.settings import override_settings


@pytest.fixture
def api_client():
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


@pytest_asyncio.fixture(autouse=True)
async def _cleanup_endpoint_runs(tmp_db):
    """The flag and read endpoints create their own runs tied to
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


async def test_flag_view_item_accepts_new_quick_categories(api_client, tmp_db):
    page = await _make_claim_page(tmp_db)

    with override_settings(enable_flag_issue=True):
        for category in (
            "factually_wrong",
            "missing_consideration",
            "reasoning_flawed",
            "scope_confused",
            "other",
        ):
            resp = await api_client.post(
                f"/api/view-items/{page.id}/flag",
                json={"category": category, "message": f"test: {category}"},
            )
            assert resp.status_code == 200, f"category={category}: {resp.text}"

    rows = await _read_flags_for_page(tmp_db, page.id)
    categories_recorded = [r["note"].split(" ", 1)[0] for r in rows]
    assert sorted(categories_recorded) == sorted(
        [
            "[factually_wrong]",
            "[missing_consideration]",
            "[reasoning_flawed]",
            "[scope_confused]",
            "[other]",
        ]
    )


async def test_undo_flag_deletes_row_and_reputation_event(api_client, tmp_db):
    page = await _make_claim_page(tmp_db)

    with override_settings(enable_flag_issue=True):
        resp = await api_client.post(
            f"/api/view-items/{page.id}/flag",
            json={"category": "factually_wrong", "message": "misclick"},
        )
        assert resp.status_code == 200
        flag_id = resp.json()["flag_id"]

        flags_before = await _read_flags_for_page(tmp_db, page.id)
        assert len(flags_before) == 1
        rep_before = (
            await tmp_db._execute(
                tmp_db.client.table("reputation_events")
                .select("*")
                .eq("project_id", tmp_db.project_id)
                .eq("dimension", "view_item_issue")
            )
        ).data
        assert len(rep_before) == 1

        undo = await api_client.delete(f"/api/view-items/flags/{flag_id}")
        assert undo.status_code == 200
        assert undo.json() == {"ok": True, "flag_id": flag_id}

    flags_after = await _read_flags_for_page(tmp_db, page.id)
    assert flags_after == []
    rep_after = (
        await tmp_db._execute(
            tmp_db.client.table("reputation_events")
            .select("*")
            .eq("project_id", tmp_db.project_id)
            .eq("dimension", "view_item_issue")
        )
    ).data
    assert rep_after == []


async def test_undo_flag_is_idempotent_for_unknown_id(api_client, tmp_db):
    with override_settings(enable_flag_issue=True):
        resp = await api_client.delete("/api/view-items/flags/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


async def test_undo_flag_returns_403_when_disabled(api_client, tmp_db):
    page = await _make_claim_page(tmp_db)
    with override_settings(enable_flag_issue=True):
        resp = await api_client.post(
            f"/api/view-items/{page.id}/flag",
            json={"category": "other", "message": "x"},
        )
        flag_id = resp.json()["flag_id"]

    with override_settings(enable_flag_issue=False):
        undo = await api_client.delete(f"/api/view-items/flags/{flag_id}")
    assert undo.status_code == 403

    flags = await _read_flags_for_page(tmp_db, page.id)
    assert len(flags) == 1


async def test_undo_flag_rejects_non_view_item_flag(api_client, tmp_db):
    page = await _make_claim_page(tmp_db)

    other_flag_id = str(_uuid.uuid4())
    await tmp_db._execute(
        tmp_db.client.table("page_flags").insert(
            {
                "id": other_flag_id,
                "flag_type": "funniness",
                "page_id": page.id,
                "call_id": None,
                "page_id_a": None,
                "page_id_b": None,
                "note": "[a different flag type]",
                "created_at": datetime.now(UTC).isoformat(),
                "run_id": tmp_db.run_id,
                "staged": tmp_db.staged,
            }
        ),
    )

    with override_settings(enable_flag_issue=True):
        undo = await api_client.delete(f"/api/view-items/flags/{other_flag_id}")
    assert undo.status_code == 400


async def test_record_view_item_read_writes_reputation_event(api_client, tmp_db):
    page = await _make_claim_page(tmp_db)

    resp = await api_client.post(
        f"/api/view-items/{page.id}/read",
        json={"seconds": 4.2},
    )
    assert resp.status_code == 200
    assert resp.json()["page_id"] == page.id

    res = await tmp_db._execute(
        tmp_db.client.table("reputation_events")
        .select("*")
        .eq("project_id", tmp_db.project_id)
        .eq("source", "human_feedback")
        .eq("dimension", "read_time")
    )
    events = res.data
    assert len(events) == 1
    assert events[0]["score"] == 1.0
    extra = events[0]["extra"]
    assert extra["subject_page_id"] == page.id
    assert extra["seconds"] == 4.2


async def test_record_view_item_read_short_id(api_client, tmp_db):
    page = await _make_claim_page(tmp_db)
    resp = await api_client.post(
        f"/api/view-items/{page.id[:8]}/read",
        json={"seconds": 2.0},
    )
    assert resp.status_code == 200
    assert resp.json()["page_id"] == page.id


async def test_record_view_item_read_404_for_unknown_page(api_client):
    resp = await api_client.post(
        "/api/view-items/00000000-0000-0000-0000-000000000000/read",
        json={"seconds": 2.0},
    )
    assert resp.status_code == 404


async def test_record_view_item_read_not_gated_by_enable_flag_issue(api_client, tmp_db):
    """Read telemetry is fine to collect even with flagging disabled -- it is
    the passive signal, not a user mutation.
    """
    page = await _make_claim_page(tmp_db)
    with override_settings(enable_flag_issue=False):
        resp = await api_client.post(
            f"/api/view-items/{page.id}/read",
            json={"seconds": 2.5},
        )
    assert resp.status_code == 200


def _basic_auth_header(password: str) -> dict[str, str]:
    token = base64.b64encode(f"user:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest_asyncio.fixture
async def _restore_auth_globals():
    """Snapshot and restore the two module-level password globals so tests can
    flip them without leaking between tests.
    """
    admin_before = app_module._AUTH_PASSWORD
    friendly_before = app_module._FRIENDLY_USER_PASSWORD
    yield
    app_module._AUTH_PASSWORD = admin_before
    app_module._FRIENDLY_USER_PASSWORD = friendly_before


async def test_middleware_admin_password_allows_everything(api_client, _restore_auth_globals):
    app_module._AUTH_PASSWORD = "admin-secret"
    app_module._FRIENDLY_USER_PASSWORD = ""

    resp = await api_client.get("/api/projects")
    assert resp.status_code == 401

    resp = await api_client.get("/api/projects", headers=_basic_auth_header("admin-secret"))
    assert resp.status_code == 200


async def test_middleware_friendly_password_allows_only_read_and_flag_paths(
    api_client, tmp_db, _restore_auth_globals
):
    page = await _make_claim_page(tmp_db)
    app_module._AUTH_PASSWORD = "admin-secret"
    app_module._FRIENDLY_USER_PASSWORD = "friend-secret"

    friendly = _basic_auth_header("friend-secret")

    assert (await api_client.get("/api/config", headers=friendly)).status_code == 200
    assert (
        await api_client.get(f"/api/questions/{page.id}/view", headers=friendly)
    ).status_code in (200, 404)
    assert (
        await api_client.get(f"/api/pages/short/{page.id[:8]}", headers=friendly)
    ).status_code == 200
    assert (await api_client.get(f"/api/pages/{page.id}", headers=friendly)).status_code == 200

    with override_settings(enable_flag_issue=True):
        flag_resp = await api_client.post(
            f"/api/view-items/{page.id}/flag",
            json={"category": "factually_wrong", "message": "x"},
            headers=friendly,
        )
        assert flag_resp.status_code == 200
        flag_id = flag_resp.json()["flag_id"]
        undo_resp = await api_client.delete(f"/api/view-items/flags/{flag_id}", headers=friendly)
        assert undo_resp.status_code == 200

    read_resp = await api_client.post(
        f"/api/view-items/{page.id}/read",
        json={"seconds": 2.0},
        headers=friendly,
    )
    assert read_resp.status_code == 200

    assert (await api_client.get("/api/projects", headers=friendly)).status_code == 401
    assert (await api_client.get("/api/ab-evals", headers=friendly)).status_code == 401
    assert (
        await api_client.get(f"/api/pages/{page.id}/detail", headers=friendly)
    ).status_code == 401
    assert (
        await api_client.get(f"/api/projects/{tmp_db.project_id}/runs", headers=friendly)
    ).status_code == 401
    assert (await api_client.post("/api/chat", json={}, headers=friendly)).status_code == 401


async def test_middleware_wrong_password_blocks_all(api_client, _restore_auth_globals):
    app_module._AUTH_PASSWORD = "admin-secret"
    app_module._FRIENDLY_USER_PASSWORD = "friend-secret"

    bad = _basic_auth_header("not-the-password")
    assert (await api_client.get("/api/config", headers=bad)).status_code == 401
    assert (await api_client.get("/api/projects", headers=bad)).status_code == 401


async def test_middleware_healthz_always_open(api_client, _restore_auth_globals):
    app_module._AUTH_PASSWORD = "admin-secret"
    app_module._FRIENDLY_USER_PASSWORD = "friend-secret"
    resp = await api_client.get("/healthz")
    assert resp.status_code == 200


async def test_middleware_no_passwords_set_everything_open(api_client, _restore_auth_globals):
    app_module._AUTH_PASSWORD = ""
    app_module._FRIENDLY_USER_PASSWORD = ""
    resp = await api_client.get("/api/projects")
    assert resp.status_code == 200


async def _count_friendly_feedback_runs(tmp_db) -> int:
    rows = (
        await tmp_db._execute(
            tmp_db.client.table("runs")
            .select("id")
            .eq("project_id", tmp_db.project_id)
            .eq("name", "friendly-user-feedback")
        )
    ).data
    return len(rows)


async def test_many_flag_and_read_posts_reuse_one_run(api_client, tmp_db):
    """Regression for C2: repeated flag/read events must not create a new
    runs row per POST. Prior behaviour was O(N) rows in the runs table
    (one per click / dwell ping) flooding /api/projects/{id}/runs with
    telemetry noise and satisfying reputation_events.run_id FK with
    disposable no-op runs. Fixed by routing through
    get_or_create_named_run(project_id, 'friendly-user-feedback').
    """
    page = await _make_claim_page(tmp_db)

    # Sanity: no feedback run yet.
    assert await _count_friendly_feedback_runs(tmp_db) == 0

    with override_settings(enable_flag_issue=True):
        for i in range(3):
            flag_resp = await api_client.post(
                f"/api/view-items/{page.id}/flag",
                json={"category": "other", "message": f"flag {i}"},
            )
            assert flag_resp.status_code == 200

        for i in range(5):
            read_resp = await api_client.post(
                f"/api/view-items/{page.id}/read",
                json={"seconds": 2.0 + i},
            )
            assert read_resp.status_code == 200

    # Across 8 POSTs there should be exactly one friendly-feedback run.
    count = await _count_friendly_feedback_runs(tmp_db)
    assert count == 1, f"expected 1 shared friendly-feedback run, got {count}"


async def test_undo_flag_only_removes_this_page_reputation_event(api_client, tmp_db):
    """Regression for C2 undo narrowing: because every flag for a project
    now shares one run_id, the undo endpoint must scope the reputation_event
    delete to the specific flagged_page_id. Otherwise undoing flag A would
    wipe the reputation_event for flag B on a different page.
    """
    page_a = await _make_claim_page(tmp_db)
    page_b = await _make_claim_page(tmp_db)

    with override_settings(enable_flag_issue=True):
        resp_a = await api_client.post(
            f"/api/view-items/{page_a.id}/flag",
            json={"category": "other", "message": "A"},
        )
        resp_b = await api_client.post(
            f"/api/view-items/{page_b.id}/flag",
            json={"category": "other", "message": "B"},
        )
        flag_a_id = resp_a.json()["flag_id"]

        undo = await api_client.delete(f"/api/view-items/flags/{flag_a_id}")
        assert undo.status_code == 200

    rep_events = (
        await tmp_db._execute(
            tmp_db.client.table("reputation_events")
            .select("*")
            .eq("project_id", tmp_db.project_id)
            .eq("dimension", "view_item_issue")
        )
    ).data
    pages_seen = {ev["extra"]["flagged_page_id"] for ev in rep_events}
    assert pages_seen == {page_b.id}, "undo should only remove the A event; B event must remain"
    _ = resp_b  # silence unused
