"""API endpoints for managing orchestrator-run Kubernetes Jobs.

The CLI (rumil.cli_client) and (eventually) the frontend POST to
`/api/jobs/orchestrator-runs` to launch a Job. Pod stdout/stderr is
surfaced via the `logs_url` field in the response (a pre-built Cloud
Logging query) — we don't proxy a long-lived log stream through this
API. Endpoints are guarded by the existing `Depends(get_current_user)`
flow, so any holder of a valid Supabase JWT (FE-issued or CLI-minted)
can submit.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from rumil.api.auth import AuthUser, get_current_user
from rumil.k8s import OrchestratorRunRequest, OrchestratorRunResponse
from rumil.k8s.submit import build_logs_url, submit_orchestrator_job

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
    return OrchestratorRunResponse(job_name=job_name, logs_url=build_logs_url(job_name))
