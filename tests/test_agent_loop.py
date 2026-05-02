"""Tests for the agent loop and LLM calling functions.

These call the real LLM (Haiku in test mode) to verify the loop
works end-to-end without coupling to Anthropic response internals.
"""

from unittest.mock import AsyncMock

import anthropic.types
import pytest
from pydantic import BaseModel, Field

from rumil.calls.common import run_agent_loop
from rumil.llm import APIResponse, Tool, structured_call, text_call
from rumil.moves.base import MoveState


async def _add(inp: dict) -> str:
    return str(inp["a"] + inp["b"])


async def _fail(inp: dict) -> str:
    raise ValueError("boom")


ADD_TOOL = Tool(
    name="add",
    description="Add two numbers and return the sum.",
    input_schema={
        "type": "object",
        "properties": {
            "a": {"type": "integer", "description": "First number"},
            "b": {"type": "integer", "description": "Second number"},
        },
        "required": ["a", "b"],
    },
    fn=_add,
)

FAIL_TOOL = Tool(
    name="fail",
    description="A tool that always fails.",
    input_schema={"type": "object", "properties": {}},
    fn=_fail,
)


@pytest.mark.llm
async def test_text_only_returns_nonempty_text(tmp_db, scout_call):
    """run_agent_loop with no tools should return text."""
    state = MoveState(scout_call, tmp_db)
    result = await run_agent_loop(
        "You are a helpful assistant.",
        "Say hello in one word.",
        tools=[],
        call_id=scout_call.id,
        db=tmp_db,
        state=state,
    )
    assert result.text.strip()
    assert result.tool_calls == []


@pytest.mark.llm
async def test_tool_is_called_and_result_recorded(tmp_db, scout_call):
    """When given a tool that matches the task, the LLM calls it."""
    state = MoveState(scout_call, tmp_db)
    result = await run_agent_loop(
        "You are a calculator. Use the add tool to answer.",
        "What is 3 + 7?",
        tools=[ADD_TOOL],
        call_id=scout_call.id,
        db=tmp_db,
        state=state,
        max_rounds=2,
    )
    assert len(result.tool_calls) >= 1
    add_call = result.tool_calls[0]
    assert add_call.name == "add"
    assert add_call.result == "10"


@pytest.mark.llm
async def test_tool_error_does_not_crash_loop(tmp_db, scout_call):
    """If a tool raises, the loop continues and returns a result."""
    state = MoveState(scout_call, tmp_db)
    result = await run_agent_loop(
        "You are a test assistant. Try the fail tool, then respond.",
        "Please call the fail tool.",
        tools=[FAIL_TOOL],
        call_id=scout_call.id,
        db=tmp_db,
        state=state,
        max_rounds=2,
    )
    assert len(result.tool_calls) >= 1
    assert "boom" in result.tool_calls[0].result


@pytest.mark.llm
async def test_max_rounds_limits_tool_calls(tmp_db, scout_call):
    """The loop should not exceed max_rounds of tool calling."""
    call_count = []

    async def ping(inp: dict) -> str:
        call_count.append(1)
        return "pong"

    ping_tool = Tool(
        name="ping",
        description="Ping. Always call this again after getting a result.",
        input_schema={"type": "object", "properties": {}},
        fn=ping,
    )

    state = MoveState(scout_call, tmp_db)
    await run_agent_loop(
        "You must call the ping tool every turn. Never stop calling it.",
        "Start pinging.",
        tools=[ping_tool],
        call_id=scout_call.id,
        db=tmp_db,
        state=state,
        max_rounds=2,
    )
    # max_rounds=2 means at most 3 rounds (0, 1, 2), so tool calls are bounded
    assert len(call_count) <= 4


async def test_end_turn_with_tool_uses_still_executes_them(tmp_db, scout_call, mocker, caplog):
    """If the API returns stop_reason=end_turn together with tool_use blocks
    (which extended-thinking Opus 4.7 has been observed to do), the agent loop
    must still execute the tool calls rather than dropping them on the floor."""
    add_calls: list[dict] = []

    async def add_recording(inp: dict) -> str:
        add_calls.append(inp)
        return str(inp["a"] + inp["b"])

    add_tool = Tool(
        name="add",
        description="Add two numbers.",
        input_schema={
            "type": "object",
            "properties": {
                "a": {"type": "integer"},
                "b": {"type": "integer"},
            },
            "required": ["a", "b"],
        },
        fn=add_recording,
    )

    end_turn_with_tool = APIResponse(
        message=anthropic.types.Message(
            id="msg_round0",
            type="message",
            role="assistant",
            content=[
                anthropic.types.TextBlock(type="text", text="Calling add."),
                anthropic.types.ToolUseBlock(
                    type="tool_use",
                    id="toolu_01",
                    name="add",
                    input={"a": 3, "b": 7},
                ),
            ],
            model="claude-opus-4-7",
            stop_reason="end_turn",
            usage=anthropic.types.Usage(input_tokens=10, output_tokens=5),
        ),
        duration_ms=1,
    )
    end_turn_no_tool = APIResponse(
        message=anthropic.types.Message(
            id="msg_round1",
            type="message",
            role="assistant",
            content=[anthropic.types.TextBlock(type="text", text="Done.")],
            model="claude-opus-4-7",
            stop_reason="end_turn",
            usage=anthropic.types.Usage(input_tokens=12, output_tokens=2),
        ),
        duration_ms=1,
    )
    mock_api = AsyncMock(side_effect=[end_turn_with_tool, end_turn_no_tool])
    mocker.patch("rumil.calls.common.call_anthropic_api", mock_api)
    mocker.patch("rumil.settings.Settings.require_anthropic_key", return_value="fake")

    state = MoveState(scout_call, tmp_db)
    with caplog.at_level("WARNING", logger="rumil.calls.common"):
        result = await run_agent_loop(
            "You are a calculator.",
            "Add 3 and 7.",
            tools=[add_tool],
            call_id=scout_call.id,
            db=tmp_db,
            state=state,
            max_rounds=3,
        )

    assert add_calls == [{"a": 3, "b": 7}], (
        "tool_use must be executed even when stop_reason is end_turn"
    )
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "add"
    assert result.tool_calls[0].result == "10"
    assert any(
        "stop_reason=end_turn" in rec.message and "pending tool call" in rec.message
        for rec in caplog.records
    ), "expected a warning when end_turn arrives with pending tool calls"


@pytest.mark.llm
async def test_text_call_returns_string():
    """text_call should return a non-empty string."""
    result = await text_call(
        "You are a helpful assistant.",
        "Say 'yes' and nothing else.",
    )
    assert isinstance(result, str)
    assert result.strip()


@pytest.mark.llm
async def test_structured_call_returns_parsed_dict():
    """structured_call should return a dict matching the response model."""

    class Rating(BaseModel):
        score: int = Field(description="A rating from 1 to 5")
        reason: str = Field(description="One-sentence reason")

    result = await structured_call(
        "You are a rating bot.",
        "Rate the color blue on a scale of 1-5.",
        response_model=Rating,
    )
    assert result.parsed is not None
    assert isinstance(result.parsed.score, int)
    assert result.parsed.reason
    assert result.input_tokens is not None
    assert result.output_tokens is not None
    assert result.duration_ms is not None
