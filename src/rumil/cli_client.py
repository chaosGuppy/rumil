"""Laptop-side HTTP client for submitting orchestrator runs as k8s Jobs.

Used by `main.py` when `--executor prod` is set. Mints a short-lived
Supabase HS256 JWT locally (using the same `SUPABASE_JWT_SECRET` already
present in prod secrets), POSTs the run spec to the rumil API, prints
the job name and a Cloud Logging URL, then exits. Pod logs are followed
via that URL rather than streamed back over HTTP.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

import httpx
import jwt

from rumil.k8s.types import OrchestratorRunRequest, OrchestratorRunResponse
from rumil.settings import get_settings

log = logging.getLogger(__name__)

_DEFAULT_JWT_TTL_S = 600


def mint_cli_jwt(*, user_id: str, secret: str, ttl_s: int = _DEFAULT_JWT_TTL_S) -> str:
    if not user_id:
        raise RuntimeError(
            "No CLI user_id configured. The default CLI service-account UUID is "
            "baked into Settings; set DEFAULT_CLI_USER_ID to override, or run "
            "scripts/create_cli_service_account.py to create the service account "
            "and update the default."
        )
    if not secret:
        raise RuntimeError(
            "SUPABASE_JWT_SECRET is not set; required for --executor prod. "
            "Use the same value as the prod cluster's rumil-api-secrets."
        )
    now = int(time.time())
    return jwt.encode(
        {
            "sub": user_id,
            "email": "cli@rumil.local",
            "aud": "authenticated",
            "role": "authenticated",
            "iat": now,
            "exp": now + ttl_s,
        },
        secret,
        algorithm="HS256",
    )


def _request_from_args(args: argparse.Namespace) -> OrchestratorRunRequest:
    question = args.question
    if isinstance(question, str) and question.endswith(".json") and Path(question).exists():
        # Structured-question file inputs aren't supported remotely yet — too
        # much new surface for v1. Users can always copy the headline text.
        raise SystemExit(
            "--executor prod does not support .json question files yet; "
            "pass the question as a plain string."
        )
    return OrchestratorRunRequest(
        question=question,
        budget=args.budget,
        workspace=args.workspace_name,
        smoke_test=bool(getattr(args, "smoke_test", False)),
        quiet=bool(getattr(args, "quiet", False)),
        debug=bool(getattr(args, "debug", False)),
        force_twophase_recurse=bool(getattr(args, "force_twophase_recurse", False)),
        no_trace=bool(getattr(args, "no_trace", False)),
        auto_summary=getattr(args, "summary_id", None) == "__auto__",
        auto_self_improve=getattr(args, "self_improve_id", None) == "__auto__",
        available_moves=getattr(args, "available_moves", None),
        available_calls=getattr(args, "available_calls", None),
        view_variant=getattr(args, "view_variant", None),
        ingest_num_claims=getattr(args, "ingest_num_claims", None),
        run_name=getattr(args, "run_name", None),
        container_tag=getattr(args, "container_tag", None),
    )


def submit_remote_orchestrator_run(args: argparse.Namespace) -> int:
    """POST the run, print the Cloud Logging URL, return 0 on success."""
    settings = get_settings()
    spec = _request_from_args(args)
    token = mint_cli_jwt(user_id=settings.default_cli_user_id, secret=settings.supabase_jwt_secret)
    base_url = settings.rumil_api_url.rstrip("/")
    headers = {"Authorization": f"Bearer {token}"}

    submit_url = f"{base_url}/api/jobs/orchestrator-runs"
    with httpx.Client(timeout=httpx.Timeout(30.0, read=30.0)) as http:
        resp = http.post(submit_url, headers=headers, json=spec.model_dump())
        if resp.status_code >= 400:
            print(
                f"failed to submit orchestrator run: {resp.status_code} {resp.text}",
                file=sys.stderr,
            )
            return 1
        parsed = OrchestratorRunResponse.model_validate(resp.json())

    print(f"submitted job: {parsed.job_name}")
    print(f"run id:        {parsed.run_id}")
    if parsed.trace_url:
        print(f"trace:         {parsed.trace_url}")
    if parsed.logs_url:
        print(f"follow logs:   {parsed.logs_url}")
    else:
        print(
            "follow logs:   (no GCP_PROJECT_ID configured on the API; "
            f"use `kubectl -n rumil logs -f -l job-name={parsed.job_name}`)"
        )
    return 0
