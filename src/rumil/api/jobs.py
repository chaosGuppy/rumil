"""API endpoints for managing orchestrator-run Kubernetes Jobs.

The CLI (rumil.cli_client) and the frontend POST to
`/api/jobs/orchestrator-runs` to launch a Job. Pod stdout/stderr is
surfaced via the `logs_url` field in the response (a pre-built Cloud
Logging query) — we don't proxy a long-lived log stream through this
API.

The frontend `/jobs` page reads `GET /api/jobs` to render a chronological
list of recent orchestrator Jobs in the cluster (status, trace link, logs
link). Endpoints are guarded by `Depends(get_current_user)` so any holder
of a valid Supabase JWT (FE-issued or CLI-minted) can submit and view.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from typing import cast

from fastapi import APIRouter, Depends, HTTPException
from kubernetes import client
from kubernetes.client.exceptions import ApiException

from rumil.api.auth import AuthUser, get_current_user
from rumil.database import DB
from rumil.k8s import OrchestratorRunRequest, OrchestratorRunResponse
from rumil.k8s.submit import (
    JOB_ANNOTATION_QUESTION,
    JOB_ANNOTATION_WORKSPACE_NAME,
    JOB_LABEL_OWNER,
    JOB_LABEL_RUN_ID,
    JOB_LABEL_RUN_KIND,
    JOB_LABEL_RUN_KIND_VALUE,
    NAMESPACE,
    _kube_clients,
    build_logs_url,
    submit_orchestrator_job,
)
from rumil.k8s.types import JobListItem, JobStatus
from rumil.settings import get_settings

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/jobs", tags=["jobs"])

_JOB_LIST_LIMIT = 200


def _build_trace_url(run_id: str) -> str:
    base = get_settings().frontend_url.rstrip("/")
    return f"{base}/traces/{run_id}"


@router.post(
    "/orchestrator-runs",
    response_model=OrchestratorRunResponse,
    status_code=201,
)
async def create_orchestrator_run(
    body: OrchestratorRunRequest,
    user: AuthUser = Depends(get_current_user),
) -> OrchestratorRunResponse:
    annotation = await _resolve_question_annotation(body)
    try:
        job_name, run_id = submit_orchestrator_job(
            body, owner_user_id=user.user_id, question_annotation=annotation
        )
    except Exception as exc:
        log.exception("orchestrator job submission failed")
        raise HTTPException(status_code=500, detail="failed to submit orchestrator job") from exc
    return OrchestratorRunResponse(
        job_name=job_name,
        run_id=run_id,
        logs_url=build_logs_url(job_name),
        trace_url=_build_trace_url(run_id),
    )


async def _resolve_question_annotation(body: OrchestratorRunRequest) -> str | None:
    """For continue runs, look up the question's headline so /jobs shows it
    instead of just the bare ID. Returns None for new-question runs (the
    submitter falls back to the user-typed question text)."""
    if not body.continue_id:
        return None
    db = await DB.create(run_id=str(uuid.uuid4()), prod=get_settings().is_prod_db)
    try:
        page = await db.get_page(body.continue_id)
    except Exception:
        log.warning("failed to resolve continue_id headline for /jobs label", exc_info=True)
        return None
    finally:
        await db.close()
    return page.headline if page and page.headline else None


@router.get("", response_model=list[JobListItem])
def list_jobs(
    _: AuthUser = Depends(get_current_user),
) -> list[JobListItem]:
    """List orchestrator Jobs (newest first), capped at 200 rows.

    Filtered to `rumil.ink/run-kind=orchestrator` so non-orchestrator Jobs
    in the namespace are excluded. Jobs that pre-date the run-id/workspace/
    question metadata rollout — or any other Job that happens to share the
    run-kind label without our full metadata — are silently skipped.
    """
    batch, _apps = _kube_clients()
    label_selector = f"{JOB_LABEL_RUN_KIND}={JOB_LABEL_RUN_KIND_VALUE}"
    try:
        result = batch.list_namespaced_job(namespace=NAMESPACE, label_selector=label_selector)
    except ApiException as exc:
        log.exception("failed to list orchestrator jobs")
        raise HTTPException(status_code=502, detail="failed to list jobs") from exc

    jobs = cast(Sequence[client.V1Job], result.items or [])
    items = [_job_to_item(j) for j in jobs if _has_orchestrator_metadata(j)]
    items.sort(key=lambda it: it.created_at, reverse=True)
    return items[:_JOB_LIST_LIMIT]


def _has_orchestrator_metadata(job: client.V1Job) -> bool:
    metadata = job.metadata
    if metadata is None:
        return False
    labels = metadata.labels or {}
    annotations = metadata.annotations or {}
    return (
        JOB_LABEL_RUN_ID in labels
        and JOB_ANNOTATION_WORKSPACE_NAME in annotations
        and JOB_ANNOTATION_QUESTION in annotations
    )


def _job_to_item(job: client.V1Job) -> JobListItem:
    metadata = job.metadata
    assert metadata is not None  # filtered upstream by _has_orchestrator_metadata
    labels = metadata.labels or {}
    annotations = metadata.annotations or {}
    name = metadata.name or ""
    run_id = labels[JOB_LABEL_RUN_ID]
    status = job.status

    return JobListItem(
        job_name=name,
        namespace=metadata.namespace or NAMESPACE,
        status=_classify_status(job),
        created_at=metadata.creation_timestamp,
        started_at=status.start_time if status else None,
        completed_at=_terminal_at(status),
        run_id=run_id,
        owner_user_id=labels.get(JOB_LABEL_OWNER, ""),
        workspace=annotations[JOB_ANNOTATION_WORKSPACE_NAME],
        question=annotations[JOB_ANNOTATION_QUESTION],
        logs_url=build_logs_url(name),
        trace_url=_build_trace_url(run_id),
    )


def _terminal_at(status: client.V1JobStatus | None):
    """Best-available timestamp for "this job stopped running".

    Kubernetes only populates ``completion_time`` on **success**. For a
    failed job ``completion_time`` stays None, so the frontend's duration
    calculation falls back to ``Date.now()`` and the row appears to run
    forever. Fall back to the latest ``Failed`` / ``Complete`` condition
    transition time so failed runs report a sensible duration.
    """
    if status is None:
        return None
    if status.completion_time is not None:
        return status.completion_time
    conditions = status.conditions or []
    terminal = [
        c
        for c in conditions
        if (c.type or "") in ("Complete", "Failed") and (c.status or "") == "True"
    ]
    if not terminal:
        return None
    terminal.sort(
        key=lambda c: c.last_transition_time or c.last_probe_time,
        reverse=True,
    )
    return terminal[0].last_transition_time


def _classify_status(job: client.V1Job) -> JobStatus:
    status = job.status
    if status is None:
        return "pending"
    for cond in status.conditions or []:
        if cond.status != "True":
            continue
        if cond.type == "Complete":
            return "completed"
        if cond.type == "Failed":
            return "failed"
    if status.active and status.active > 0:
        return "running"
    return "pending"
