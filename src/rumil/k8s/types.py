"""Shared request/response models for the orchestrator-run job flow.

These types travel over the wire between the laptop CLI (`rumil.cli_client`)
and the API router (`rumil.api.jobs`), and are also consumed by the in-cluster
submitter (`rumil.k8s.submit`). Kept FastAPI-free so the CLI can import them
without dragging server-side modules.
"""

from pydantic import BaseModel, Field

# Docker tag character set (https://docs.docker.com/reference/cli/docker/image/tag/),
# bounded for safety. Validates the user-supplied container_tag so we never
# splice an attacker-controlled value into a Job's image string.
_CONTAINER_TAG_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"


class OrchestratorRunRequest(BaseModel):
    """Inputs for a remote orchestrator run.

    Mirrors the subset of `main.py` flags that are forwarded into the
    in-pod CLI invocation. Flags that only make sense on the laptop
    (--db, --executor, --staged, --env-file, --run-id-file, --cli-user-id)
    are intentionally absent — the in-pod run is always `--db prod
    --executor local`.
    """

    question: str = Field(min_length=1)
    budget: int = Field(ge=1)
    workspace: str = Field(min_length=1)

    smoke_test: bool = False
    no_summary: bool = False
    quiet: bool = False
    debug: bool = False
    force_twophase_recurse: bool = False
    no_trace: bool = False

    available_moves: str | None = None
    available_calls: str | None = None
    view_variant: str | None = None
    ingest_num_claims: int | None = None
    run_name: str | None = None

    # Image tag override for experiment runs. Only the tag portion of the
    # image is replaced; the registry/repo is always read from the live
    # rumil-api Deployment so a caller cannot point the Job at an arbitrary
    # registry. Used by scripts/remote_run.sh.
    container_tag: str | None = Field(default=None, pattern=_CONTAINER_TAG_PATTERN)


class OrchestratorRunResponse(BaseModel):
    job_name: str
