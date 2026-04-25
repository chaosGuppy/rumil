"""API endpoints for managing orchestrator-run Kubernetes Jobs.

The CLI (rumil.cli_client) and (eventually) the frontend POST to
`/api/jobs/orchestrator-runs` to launch a Job; logs are streamed back via
`/api/jobs/orchestrator-runs/{name}/logs`. Both endpoints are guarded by
the existing `Depends(get_current_user)` flow, so any holder of a valid
Supabase JWT (FE-issued or CLI-minted) can submit.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from rumil.api.auth import AuthUser, get_current_user
from rumil.k8s import OrchestratorRunRequest, OrchestratorRunResponse
from rumil.k8s.submit import (
    get_orchestrator_job,
    stream_job_logs,
    submit_orchestrator_job,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.post(
    "/orchestrator-runs",
    response_model=OrchestratorRunResponse,
    status_code=201,
)
def create_orchestrator_run(
    body: OrchestratorRunRequest,
    user: AuthUser = Depends(get_current_user),
) -> OrchestratorRunResponse:
    try:
        job_name = submit_orchestrator_job(body, owner_user_id=user.user_id)
    except Exception as exc:
        log.exception("orchestrator job submission failed")
        raise HTTPException(status_code=500, detail="failed to submit orchestrator job") from exc
    return OrchestratorRunResponse(job_name=job_name)


@router.get("/orchestrator-runs/{job_name}/logs")
def stream_orchestrator_run_logs(
    job_name: str,
    _user: AuthUser = Depends(get_current_user),
) -> StreamingResponse:
    job = get_orchestrator_job(job_name)
    if job is None:
        raise HTTPException(status_code=404, detail="orchestrator run not found")
    return StreamingResponse(stream_job_logs(job_name), media_type="text/plain")
