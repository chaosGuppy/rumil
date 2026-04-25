"""Laptop-side CLI client: JWT minting, exit-code parsing, end-to-end submit flow."""

from __future__ import annotations

import argparse
import time

import httpx
import jwt
import pytest

from rumil.cli_client import (
    _parse_exit_code,
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


def test_parse_exit_code_finds_marker_in_tail():
    tail = b"some logs\n[rumil-job] phase=Succeeded exit_code=0\n"
    assert _parse_exit_code(tail) == 0


def test_parse_exit_code_handles_failure():
    tail = b"[rumil-job] phase=Failed exit_code=2\n"
    assert _parse_exit_code(tail) == 2


def test_parse_exit_code_returns_one_when_marker_missing():
    tail = b"truncated stream..."
    assert _parse_exit_code(tail) == 1


def test_parse_exit_code_returns_one_when_marker_garbled():
    tail = b"[rumil-job] phase=X exit_code=oops\n"
    assert _parse_exit_code(tail) == 1


def _args_namespace(**overrides) -> argparse.Namespace:
    base: dict = {
        "question": "is the sky blue?",
        "budget": 1,
        "workspace_name": "ws",
        "smoke_test": False,
        "no_summary": False,
        "quiet": False,
        "debug": False,
        "force_twophase_recurse": False,
        "no_trace": False,
        "available_moves": None,
        "available_calls": None,
        "view_variant": None,
        "ingest_num_claims": None,
        "run_name": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


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


def test_submit_round_trip_streams_logs_and_returns_exit_code(mocker, capsys):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/orchestrator-runs"):
            captured["body"] = request.content
            captured["auth"] = request.headers.get("authorization")
            return httpx.Response(201, json={"job_name": "rumil-orch-ws-deadbeef"})
        if request.method == "GET" and "/logs" in request.url.path:
            body = b"hello from the pod\n[rumil-job] phase=Succeeded exit_code=0\n"
            return httpx.Response(200, content=body)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    real_client_cls = httpx.Client

    def _patched_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client_cls(*args, **kwargs)

    mocker.patch("rumil.cli_client.httpx.Client", side_effect=_patched_client)

    args = _args_namespace()
    with override_settings(
        rumil_api_url="http://api.test",
        default_cli_user_id="u-1",
        supabase_jwt_secret="s" * 40,
    ):
        rc = submit_remote_orchestrator_run(args)

    assert rc == 0
    assert captured["auth"].startswith("Bearer ")
    captured_out = capsys.readouterr()
    assert "hello from the pod" in captured_out.out


def test_submit_returns_nonzero_on_post_failure(mocker, capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    transport = httpx.MockTransport(handler)
    real_client_cls = httpx.Client

    def _patched_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client_cls(*args, **kwargs)

    mocker.patch("rumil.cli_client.httpx.Client", side_effect=_patched_client)

    with override_settings(
        rumil_api_url="http://api.test",
        default_cli_user_id="u-1",
        supabase_jwt_secret="s" * 40,
    ):
        rc = submit_remote_orchestrator_run(_args_namespace())
    assert rc == 1
