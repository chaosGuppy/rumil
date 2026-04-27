"""Tests for the /api/jobs router (POST orchestrator-runs and GET list)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from kubernetes import client as kclient

from rumil.api.app import app
from rumil.api.auth import AuthUser, get_current_user
from rumil.api.jobs import _classify_status, _has_orchestrator_metadata, _job_to_item
from rumil.k8s.submit import (
    JOB_ANNOTATION_QUESTION,
    JOB_ANNOTATION_WORKSPACE_NAME,
    JOB_LABEL_OWNER,
    JOB_LABEL_RUN_ID,
    JOB_LABEL_RUN_KIND,
    JOB_LABEL_RUN_KIND_VALUE,
    JOB_LABEL_WORKSPACE,
)


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


_FAKE_RUN_ID = "11111111-1111-1111-1111-111111111111"


@pytest.mark.asyncio
async def test_post_orchestrator_run_returns_job_name_and_logs_url(mocker, auth_overridden_client):
    fake_submit = mocker.patch(
        "rumil.api.jobs.submit_orchestrator_job",
        return_value=("rumil-orch-ws-cafebabe", _FAKE_RUN_ID),
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
    assert payload["run_id"] == _FAKE_RUN_ID
    assert payload["logs_url"].startswith("https://console.cloud.google.com/logs/query")
    assert payload["trace_url"].endswith(f"/traces/{_FAKE_RUN_ID}")

    fake_submit.assert_called_once()
    _, kwargs = fake_submit.call_args
    assert kwargs["owner_user_id"] == "user-123"


@pytest.mark.asyncio
async def test_post_orchestrator_run_returns_empty_logs_url_when_no_project(
    mocker, auth_overridden_client
):
    mocker.patch(
        "rumil.api.jobs.submit_orchestrator_job",
        return_value=("rumil-orch-ws-1", _FAKE_RUN_ID),
    )
    mocker.patch("rumil.api.jobs.build_logs_url", return_value="")
    resp = await auth_overridden_client.post(
        "/api/jobs/orchestrator-runs",
        json={"question": "q", "budget": 1, "workspace": "ws"},
    )
    assert resp.status_code == 201
    payload = resp.json()
    assert payload["job_name"] == "rumil-orch-ws-1"
    assert payload["run_id"] == _FAKE_RUN_ID
    assert payload["logs_url"] == ""


@pytest.mark.asyncio
async def test_post_orchestrator_run_validates_request_body(mocker, auth_overridden_client):
    mocker.patch(
        "rumil.api.jobs.submit_orchestrator_job",
        return_value=("should-not-be-called", _FAKE_RUN_ID),
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


def _make_job(
    *,
    name: str = "rumil-orch-ws-aaaa",
    run_id: str = "rid-1",
    workspace_label: str = "ws",
    workspace_name: str = "ws",
    question: str = "q",
    owner: str = "user-123",
    created_at: datetime | None = None,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
    conditions: list[kclient.V1JobCondition] | None = None,
    active: int | None = None,
) -> kclient.V1Job:
    labels = {
        JOB_LABEL_RUN_KIND: JOB_LABEL_RUN_KIND_VALUE,
        JOB_LABEL_RUN_ID: run_id,
        JOB_LABEL_WORKSPACE: workspace_label,
        JOB_LABEL_OWNER: owner,
    }
    annotations = {
        JOB_ANNOTATION_WORKSPACE_NAME: workspace_name,
        JOB_ANNOTATION_QUESTION: question,
    }
    metadata = kclient.V1ObjectMeta(
        name=name,
        namespace="rumil",
        labels=labels,
        annotations=annotations,
        creation_timestamp=created_at or datetime(2026, 4, 27, 10, 0, tzinfo=UTC),
    )
    status = kclient.V1JobStatus(
        active=active,
        start_time=started_at,
        completion_time=completed_at,
        conditions=conditions,
    )
    return kclient.V1Job(metadata=metadata, status=status)


def test_classify_status_completed():
    job = _make_job(conditions=[kclient.V1JobCondition(type="Complete", status="True")])
    assert _classify_status(job) == "completed"


def test_classify_status_failed():
    job = _make_job(conditions=[kclient.V1JobCondition(type="Failed", status="True")])
    assert _classify_status(job) == "failed"


def test_classify_status_running():
    job = _make_job(active=1)
    assert _classify_status(job) == "running"


def test_classify_status_pending_default():
    assert _classify_status(_make_job()) == "pending"


def test_classify_status_ignores_false_conditions():
    """Stale False conditions must not flip the status."""
    job = _make_job(
        conditions=[kclient.V1JobCondition(type="Complete", status="False")],
        active=1,
    )
    assert _classify_status(job) == "running"


def test_job_to_item_reads_metadata_only_no_name_parsing():
    job = _make_job(
        name="rumil-orch-anything-1234",
        workspace_label="my-research",
        workspace_name="My Research",
        question="why is the sky blue?",
        run_id="rid-xyz",
    )
    item = _job_to_item(job)
    assert item.workspace == "My Research"  # from annotation, not parsed from name
    assert item.question == "why is the sky blue?"
    assert item.run_id == "rid-xyz"
    assert item.trace_url and "/traces/rid-xyz" in item.trace_url


def test_has_orchestrator_metadata_accepts_full_metadata():
    assert _has_orchestrator_metadata(_make_job()) is True


def test_has_orchestrator_metadata_rejects_missing_run_id():
    job = _make_job()
    assert job.metadata is not None
    job.metadata.labels = {JOB_LABEL_RUN_KIND: JOB_LABEL_RUN_KIND_VALUE}
    assert _has_orchestrator_metadata(job) is False


def test_has_orchestrator_metadata_rejects_missing_annotations():
    job = _make_job()
    assert job.metadata is not None
    job.metadata.annotations = None
    assert _has_orchestrator_metadata(job) is False


@pytest.mark.asyncio
async def test_get_jobs_skips_jobs_missing_metadata(mocker, auth_overridden_client):
    """Pre-rollout or hand-applied Jobs without the new metadata are silently filtered."""
    good = _make_job(name="rumil-orch-ws-good")
    stale = _make_job(name="rumil-orch-ws-stale")
    assert stale.metadata is not None
    stale.metadata.labels = {JOB_LABEL_RUN_KIND: JOB_LABEL_RUN_KIND_VALUE}
    stale.metadata.annotations = None

    fake_batch = mocker.MagicMock()
    fake_batch.list_namespaced_job.return_value = kclient.V1JobList(items=[good, stale])
    mocker.patch("rumil.api.jobs._kube_clients", return_value=(fake_batch, mocker.MagicMock()))
    resp = await auth_overridden_client.get("/api/jobs")
    assert resp.status_code == 200
    assert [row["job_name"] for row in resp.json()] == ["rumil-orch-ws-good"]


@pytest.mark.asyncio
async def test_get_jobs_lists_with_label_selector(mocker, auth_overridden_client):
    fake_batch = mocker.MagicMock()
    fake_batch.list_namespaced_job.return_value = kclient.V1JobList(
        items=[
            _make_job(
                name="rumil-orch-ws-newer",
                created_at=datetime(2026, 4, 27, 12, 0, tzinfo=UTC),
            ),
            _make_job(
                name="rumil-orch-ws-older",
                created_at=datetime(2026, 4, 27, 10, 0, tzinfo=UTC),
            ),
        ]
    )
    mocker.patch("rumil.api.jobs._kube_clients", return_value=(fake_batch, mocker.MagicMock()))
    resp = await auth_overridden_client.get("/api/jobs")
    assert resp.status_code == 200
    body = resp.json()
    assert [row["job_name"] for row in body] == [
        "rumil-orch-ws-newer",
        "rumil-orch-ws-older",
    ]
    # Filter scope: must request only orchestrator jobs, not all jobs in ns.
    _, kwargs = fake_batch.list_namespaced_job.call_args
    assert kwargs["namespace"] == "rumil"
    assert kwargs["label_selector"] == f"{JOB_LABEL_RUN_KIND}={JOB_LABEL_RUN_KIND_VALUE}"


@pytest.mark.asyncio
async def test_get_jobs_unauthorized_without_token():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as anon:
        resp = await anon.get("/api/jobs")
    assert resp.status_code == 401
