"""Laptop-side CLI client: JWT minting and end-to-end submit flow."""

from __future__ import annotations

import argparse
import time

import httpx
import jwt
import pytest

from rumil.cli_client import (
    _request_from_args,
    mint_cli_jwt,
    submit_remote_orchestrator_run,
)
from rumil.settings import override_settings


def test_mint_cli_jwt_includes_required_claims():
    secret = "x" * 40
    token = mint_cli_jwt(user_id="user-1", secret=secret, ttl_s=300)
    claims = jwt.decode(token, secret, algorithms=["HS256"], audience="authenticated")
    assert claims["sub"] == "user-1"
    assert claims["aud"] == "authenticated"
    assert claims["role"] == "authenticated"
    now = int(time.time())
    assert claims["exp"] - now <= 300 + 5
    assert claims["exp"] - now > 0


def test_mint_cli_jwt_rejects_missing_user_id():
    with pytest.raises(RuntimeError, match="user_id"):
        mint_cli_jwt(user_id="", secret="s" * 40)


def test_mint_cli_jwt_rejects_missing_secret():
    with pytest.raises(RuntimeError, match="SUPABASE_JWT_SECRET"):
        mint_cli_jwt(user_id="u", secret="")


def _args_namespace(**overrides) -> argparse.Namespace:
    base: dict = {
        "question": "is the sky blue?",
        "continue_id": None,
        "budget": 1,
        "workspace_name": "ws",
        "smoke_test": False,
        "quiet": False,
        "debug": False,
        "force_twophase_recurse": False,
        "no_trace": False,
        "summary_id": None,
        "self_improve_id": None,
        "available_moves": None,
        "available_calls": None,
        "view_variant": None,
        "ingest_num_claims": None,
        "run_name": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def test_request_from_args_forwards_continue_id():
    """`--continue X --prod` must reach the API with continue_id set and no
    question, so the in-pod CLI runs `--continue X` instead of treating
    the ID as a positional question."""
    args = _args_namespace(question=None, continue_id="qid-abcdef")
    spec = _request_from_args(args)
    assert spec.continue_id == "qid-abcdef"
    assert spec.question is None


def test_request_from_args_maps_field_names():
    args = _args_namespace(workspace_name="my-ws", smoke_test=True)
    spec = _request_from_args(args)
    assert spec.workspace == "my-ws"
    assert spec.budget == 1
    assert spec.smoke_test is True


def test_request_from_args_forwards_container_tag():
    args = _args_namespace(container_tag="exp-deadbeef")
    spec = _request_from_args(args)
    assert spec.container_tag == "exp-deadbeef"


def test_request_from_args_no_container_tag_by_default():
    args = _args_namespace()
    spec = _request_from_args(args)
    assert spec.container_tag is None


def test_request_from_args_forwards_auto_summary():
    """`--summary` (no ID) sets summary_id="__auto__"; should map to auto_summary=True."""
    args = _args_namespace(summary_id="__auto__")
    spec = _request_from_args(args)
    assert spec.auto_summary is True
    assert spec.auto_self_improve is False


def test_request_from_args_forwards_auto_self_improve():
    args = _args_namespace(self_improve_id="__auto__")
    spec = _request_from_args(args)
    assert spec.auto_self_improve is True
    assert spec.auto_summary is False


def test_request_from_args_forwards_improvement_instructions():
    args = _args_namespace(
        self_improve_id="__auto__",
        improvement_instructions="focus on prioritization quality",
    )
    spec = _request_from_args(args)
    assert spec.improvement_instructions == "focus on prioritization quality"


def test_request_from_args_no_improvement_instructions_by_default():
    spec = _request_from_args(_args_namespace())
    assert spec.improvement_instructions is None


def test_request_from_args_ignores_summary_with_real_id():
    """`--summary <QID>` is the standalone (non-orchestrator) mode and is
    rejected before it reaches the request builder; defensively, a real ID
    should not flip auto_summary."""
    args = _args_namespace(summary_id="some-question-id")
    spec = _request_from_args(args)
    assert spec.auto_summary is False


def test_request_from_args_no_auto_flags_by_default():
    spec = _request_from_args(_args_namespace())
    assert spec.auto_summary is False
    assert spec.auto_self_improve is False


def test_request_from_args_rejects_json_question():
    """Structured-question file inputs aren't supported remotely yet."""
    import os
    import tempfile

    fd, path = tempfile.mkstemp(suffix=".json")
    os.write(fd, b'{"headline":"x"}')
    os.close(fd)
    try:
        with pytest.raises(SystemExit):
            _request_from_args(_args_namespace(question=path))
    finally:
        os.unlink(path)


def _patched_httpx_client_factory(transport: httpx.MockTransport):
    """httpx.Client wrapped so it always uses our mock transport."""
    real_client_cls = httpx.Client

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client_cls(*args, **kwargs)

    return factory


def test_submit_prints_logs_url_when_present(mocker, capsys):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = request.content
        return httpx.Response(
            201,
            json={
                "job_name": "rumil-orch-ws-deadbeef",
                "run_id": "00000000-0000-0000-0000-00000000abcd",
                "logs_url": "https://console.cloud.google.com/logs/query;query=foo?project=p",
                "trace_url": "http://app.test/traces/00000000-0000-0000-0000-00000000abcd",
            },
        )

    mocker.patch(
        "rumil.cli_client.httpx.Client",
        side_effect=_patched_httpx_client_factory(httpx.MockTransport(handler)),
    )
    with override_settings(
        rumil_api_url="http://api.test",
        default_cli_user_id="u-1",
        supabase_jwt_secret="s" * 40,
    ):
        rc = submit_remote_orchestrator_run(_args_namespace())

    assert rc == 0
    assert captured["auth"].startswith("Bearer ")
    out = capsys.readouterr().out
    assert "rumil-orch-ws-deadbeef" in out
    assert "console.cloud.google.com/logs/query" in out


def test_submit_prints_kubectl_fallback_when_logs_url_empty(mocker, capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            json={
                "job_name": "rumil-orch-ws-cafe",
                "run_id": "00000000-0000-0000-0000-00000000cafe",
                "logs_url": "",
            },
        )

    mocker.patch(
        "rumil.cli_client.httpx.Client",
        side_effect=_patched_httpx_client_factory(httpx.MockTransport(handler)),
    )
    with override_settings(
        rumil_api_url="http://api.test",
        default_cli_user_id="u-1",
        supabase_jwt_secret="s" * 40,
    ):
        rc = submit_remote_orchestrator_run(_args_namespace())

    assert rc == 0
    out = capsys.readouterr().out
    assert "kubectl -n rumil logs" in out
    assert "job-name=rumil-orch-ws-cafe" in out


def test_submit_returns_nonzero_on_post_failure(mocker, capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    mocker.patch(
        "rumil.cli_client.httpx.Client",
        side_effect=_patched_httpx_client_factory(httpx.MockTransport(handler)),
    )

    with override_settings(
        rumil_api_url="http://api.test",
        default_cli_user_id="u-1",
        supabase_jwt_secret="s" * 40,
    ):
        rc = submit_remote_orchestrator_run(_args_namespace())
    assert rc == 1
