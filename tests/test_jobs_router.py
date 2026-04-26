"""Tests for the /api/jobs/orchestrator-runs router."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from rumil.api.app import app
from rumil.api.auth import AuthUser, get_current_user


@pytest.fixture
def auth_overridden_client():
    app.dependency_overrides[get_current_user] = lambda: AuthUser(
        user_id="user-123", email="t@e.com"
    )
    transport = ASGITransport(app=app)
    client = AsyncClient(transport=transport, base_url="http://test")
    try:
        yield client
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_post_orchestrator_run_returns_job_name_and_logs_url(mocker, auth_overridden_client):
    fake_submit = mocker.patch(
        "rumil.api.jobs.submit_orchestrator_job",
        return_value="rumil-orch-ws-cafebabe",
    )
    mocker.patch(
        "rumil.api.jobs.build_logs_url",
        return_value="https://console.cloud.google.com/logs/query;query=foo?project=p",
    )
    body = {"question": "is the sky blue?", "budget": 1, "workspace": "ws"}
    resp = await auth_overridden_client.post("/api/jobs/orchestrator-runs", json=body)
    assert resp.status_code == 201
    payload = resp.json()
    assert payload["job_name"] == "rumil-orch-ws-cafebabe"
    assert payload["logs_url"].startswith("https://console.cloud.google.com/logs/query")

    fake_submit.assert_called_once()
    _, kwargs = fake_submit.call_args
    assert kwargs["owner_user_id"] == "user-123"


@pytest.mark.asyncio
async def test_post_orchestrator_run_returns_empty_logs_url_when_no_project(
    mocker, auth_overridden_client
):
    mocker.patch("rumil.api.jobs.submit_orchestrator_job", return_value="rumil-orch-ws-1")
    mocker.patch("rumil.api.jobs.build_logs_url", return_value="")
    resp = await auth_overridden_client.post(
        "/api/jobs/orchestrator-runs",
        json={"question": "q", "budget": 1, "workspace": "ws"},
    )
    assert resp.status_code == 201
    assert resp.json() == {"job_name": "rumil-orch-ws-1", "logs_url": ""}


@pytest.mark.asyncio
async def test_post_orchestrator_run_validates_request_body(mocker, auth_overridden_client):
    mocker.patch(
        "rumil.api.jobs.submit_orchestrator_job",
        return_value="should-not-be-called",
    )
    resp = await auth_overridden_client.post(
        "/api/jobs/orchestrator-runs",
        json={"question": "", "budget": 1, "workspace": "ws"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_post_orchestrator_run_returns_500_when_submitter_fails(
    mocker, auth_overridden_client
):
    mocker.patch(
        "rumil.api.jobs.submit_orchestrator_job",
        side_effect=RuntimeError("kube broken"),
    )
    resp = await auth_overridden_client.post(
        "/api/jobs/orchestrator-runs",
        json={"question": "q", "budget": 1, "workspace": "ws"},
    )
    assert resp.status_code == 500


@pytest.mark.asyncio
async def test_post_orchestrator_run_unauthorized_without_token():
    """No dependency override -> JWT path runs, returns 401 without bearer."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as anon:
        resp = await anon.post(
            "/api/jobs/orchestrator-runs",
            json={"question": "q", "budget": 1, "workspace": "ws"},
        )
    assert resp.status_code == 401
