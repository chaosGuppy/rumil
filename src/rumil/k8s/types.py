"""Shared request/response models for the orchestrator-run job flow.

These types travel over the wire between the laptop CLI (`rumil.cli_client`)
and the API router (`rumil.api.jobs`), and are also consumed by the in-cluster
submitter (`rumil.k8s.submit`). Kept FastAPI-free so the CLI can import them
without dragging server-side modules.
"""

import re
from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, Field, field_validator, model_validator

from rumil.settings import Settings

JobStatus = Literal["pending", "running", "failed", "completed"]

# Docker tag character set (https://docs.docker.com/reference/cli/docker/image/tag/),
# bounded for safety. Validates the user-supplied container_tag so we never
# splice an attacker-controlled value into a Job's image string.
_CONTAINER_TAG_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"

_ENV_VAR_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


class OrchestratorRunRequest(BaseModel):
    """Inputs for a remote orchestrator run.

    Mirrors the subset of `main.py` flags that are forwarded into the
    in-pod CLI invocation. Flags that only make sense on the laptop
    (--db, --executor, --staged, --env-file, --run-id-file, --cli-user-id)
    are intentionally absent — the in-pod run is always `--db prod
    --executor local`.

    Exactly one of `question` (start a new investigation) or
    `continue_id` (resume an existing question by ID) must be set.
    """

    question: str | None = Field(default=None, min_length=1)
    continue_id: str | None = Field(default=None, min_length=1)
    budget: int = Field(ge=1)
    workspace: str = Field(min_length=1)

    smoke_test: bool = False
    quiet: bool = False
    debug: bool = False
    force_twophase_recurse: bool = False
    no_trace: bool = False
    # Post-orchestrator modifiers triggered by `--summary` / `--self-improve`
    # passed without an ID (= dest="__auto__"). These are auto-mode booleans
    # rather than ID-bearing fields because the standalone forms (with an ID)
    # are non-orchestrator modes and aren't supported remotely.
    auto_summary: bool = False
    auto_self_improve: bool = False
    # Optional steering string for --self-improve. Only meaningful when
    # auto_self_improve is True; ignored otherwise.
    improvement_instructions: str | None = None

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

    # Per-request env-var overrides. Keys must be uppercase env-var names
    # corresponding to a known Settings field; values land verbatim in the
    # spawned Job's container env, shadowing the values inherited from the
    # rumil-api Deployment. This is the trust boundary for cloud-job config
    # — the API accepts any Settings field name and trusts authenticated
    # callers not to abuse it. The CLI restricts itself to a curated subset
    # via `Settings.cli_forwardable_overrides()`; other clients are free to
    # send anything.
    extra_env: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _exactly_one_of_question_or_continue_id(self) -> Self:
        if bool(self.question) == bool(self.continue_id):
            raise ValueError("exactly one of `question` or `continue_id` must be set")
        return self

    @field_validator("extra_env")
    @classmethod
    def _validate_extra_env(cls, v: dict[str, str]) -> dict[str, str]:
        if not v:
            return v
        known = Settings.all_env_keys()
        for key, value in v.items():
            if not _ENV_VAR_NAME.match(key):
                raise ValueError(
                    f"extra_env key {key!r} must match {_ENV_VAR_NAME.pattern} "
                    "(uppercase env-var name)"
                )
            if key not in known:
                raise ValueError(f"extra_env key {key!r} is not a known Settings field")
            if not value:
                raise ValueError(
                    f"extra_env value for {key!r} must be non-empty; "
                    "omit the key to fall back to the inherited default"
                )
        return v


class OrchestratorRunResponse(BaseModel):
    job_name: str
    run_id: str
    # Pre-built Cloud Logging URL filtered to this job's pod logs. Empty when
    # the API can't determine the GCP project (e.g. local dev).
    logs_url: str = ""
    # Pre-built trace URL: <frontend>/traces/<run_id>.
    trace_url: str = ""


class JobListItem(BaseModel):
    """One row in GET /api/jobs. Built from a V1Job's metadata only."""

    job_name: str
    namespace: str
    status: JobStatus
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    run_id: str
    workspace: str
    question: str
    trace_url: str
    owner_user_id: str = ""
    # Empty when the API can't determine the GCP project (e.g. local dev).
    logs_url: str = ""
