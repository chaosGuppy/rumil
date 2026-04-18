"""Regression tests for the thinking-config wiring in run_sdk_agent.

Background: Opus 4.7 / 4.6 / Sonnet 4.6 require ``thinking.type.adaptive`` on the
Anthropic Messages API. The bundled Claude CLI historically defaulted to
``thinking.type.enabled``, which those models reject with HTTP 400. rumil's
sdk_agent must therefore (a) pass ``thinking={"type": "adaptive"}`` explicitly
for adaptive-capable models, and (b) disable thinking for models that don't
support adaptive so no CLI default can resurrect the bug.

These tests never touch the network — they intercept the ``ClaudeAgentOptions``
handed to a mocked ``ClaudeSDKClient``.
"""

import uuid
from dataclasses import dataclass
from typing import Any

import pytest

from rumil.models import Call, CallStatus, CallType, Workspace
from rumil.sdk_agent import SdkAgentConfig, run_sdk_agent
from rumil.tracing.tracer import CallTrace


@dataclass
class _StubSettings:
    """Minimal settings stub — only reads what run_sdk_agent touches."""

    model: str
    sdk_agent_max_subagents: int = 3
    sdk_agent_max_turns: int = 200


class _FakeSDKClient:
    """Async context-manager stand-in for ClaudeSDKClient.

    Records the ``options`` arg it receives so tests can assert on it, and
    yields a single ResultMessage so run_sdk_agent can complete cleanly.
    """

    captured_options: Any = None

    def __init__(self, options: Any) -> None:
        type(self).captured_options = options

    async def __aenter__(self) -> "_FakeSDKClient":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def query(self, _prompt: str) -> None:
        return None

    async def receive_response(self):
        from claude_agent_sdk import ResultMessage

        yield ResultMessage(
            subtype="success",
            duration_ms=0,
            duration_api_ms=0,
            is_error=False,
            num_turns=0,
            session_id="test",
            total_cost_usd=0.0,
            usage=None,
            result="done",
        )


async def _run_with_model(model: str, tmp_db, mocker) -> Any:
    """Drive run_sdk_agent under ``model`` and return the captured options."""
    _FakeSDKClient.captured_options = None
    mocker.patch(
        "rumil.sdk_agent.get_settings",
        return_value=_StubSettings(model=model),
    )

    scope_page_id = "scope-" + str(uuid.uuid4())
    call = Call(
        id=str(uuid.uuid4()),
        call_type=CallType.RUN_EVAL,
        workspace=Workspace.RESEARCH,
        scope_page_id=scope_page_id,
        status=CallStatus.PENDING,
    )

    trace = CallTrace(call.id, tmp_db)
    config = SdkAgentConfig(
        system_prompt="sys",
        user_prompt="user",
        server_name="test-tools",
        mcp_tools=[],
        call=call,
        call_type=CallType.RUN_EVAL,
        scope_page_id=scope_page_id,
        db=tmp_db,
        trace=trace,
    )

    await run_sdk_agent(config)

    return _FakeSDKClient.captured_options


@pytest.fixture
def patch_sdk(mocker):
    """Replace the SDK client and MCP server factory imported into sdk_agent."""
    mocker.patch("rumil.sdk_agent.ClaudeSDKClient", _FakeSDKClient)
    mocker.patch(
        "rumil.sdk_agent.create_sdk_mcp_server",
        return_value=object(),
    )


async def test_opus_4_7_passes_adaptive_thinking(tmp_db, patch_sdk, mocker):
    options = await _run_with_model("claude-opus-4-7", tmp_db, mocker)

    assert options is not None, "ClaudeSDKClient was not constructed"
    assert options.thinking == {"type": "adaptive"}
    # Adaptive must take precedence over the legacy token-budget field.
    assert options.max_thinking_tokens is None


async def test_sonnet_4_6_passes_adaptive_thinking(tmp_db, patch_sdk, mocker):
    options = await _run_with_model("claude-sonnet-4-6-20260101", tmp_db, mocker)

    assert options.thinking == {"type": "adaptive"}
    assert options.max_thinking_tokens is None


async def test_haiku_disables_thinking(tmp_db, patch_sdk, mocker):
    options = await _run_with_model("claude-haiku-4-5-20251001", tmp_db, mocker)

    assert options.thinking is None
    assert options.max_thinking_tokens == 0
