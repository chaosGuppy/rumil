"""Laptop-side HTTP client for submitting orchestrator runs as k8s Jobs.

Used by `main.py` when `--executor prod` is set. Mints a short-lived
Supabase HS256 JWT locally (using the same `SUPABASE_JWT_SECRET` already
present in prod secrets), POSTs the run spec to the rumil API, then
streams pod logs back to stdout until the marker line tells us the job
finished. The CLI's exit code mirrors the in-pod exit code.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from pathlib import Path

import httpx
import jwt

from rumil.k8s.types import OrchestratorRunRequest, OrchestratorRunResponse
from rumil.settings import get_settings

log = logging.getLogger(__name__)

_TERMINAL_MARKER = re.compile(rb"\[rumil-job\] phase=(\S+) exit_code=(\S+)")
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
        no_summary=bool(getattr(args, "no_summary", False)),
        quiet=bool(getattr(args, "quiet", False)),
        debug=bool(getattr(args, "debug", False)),
        force_twophase_recurse=bool(getattr(args, "force_twophase_recurse", False)),
        no_trace=bool(getattr(args, "no_trace", False)),
        available_moves=getattr(args, "available_moves", None),
        available_calls=getattr(args, "available_calls", None),
        view_variant=getattr(args, "view_variant", None),
        ingest_num_claims=getattr(args, "ingest_num_claims", None),
        run_name=getattr(args, "run_name", None),
        container_tag=getattr(args, "container_tag", None),
    )


def _parse_exit_code(buf: bytes) -> int:
    m = _TERMINAL_MARKER.search(buf)
    if not m:
        return 1
    code_raw = m.group(2).decode("utf-8", errors="replace")
    try:
        return int(code_raw)
    except ValueError:
        return 1


def submit_remote_orchestrator_run(args: argparse.Namespace) -> int:
    """POST the run, stream logs to stdout, return the in-pod exit code."""
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
        job_name = parsed.job_name
    print(f"submitted job {job_name}", file=sys.stderr)

    logs_url = f"{base_url}/api/jobs/orchestrator-runs/{job_name}/logs"
    tail = bytearray()
    # No top-level read timeout: orchestrator runs can be hours.
    with (
        httpx.Client(timeout=httpx.Timeout(30.0, read=None)) as http,
        http.stream("GET", logs_url, headers=headers) as resp,
    ):
        if resp.status_code >= 400:
            print(
                f"failed to stream logs: {resp.status_code} {resp.read().decode(errors='replace')}",
                file=sys.stderr,
            )
            return 1
        for chunk in resp.iter_bytes():
            if not chunk:
                continue
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
            tail.extend(chunk)
            if len(tail) > 4096:
                del tail[:-4096]

    return _parse_exit_code(bytes(tail))
