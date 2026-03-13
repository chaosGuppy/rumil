"""Tests for the agent loop and LLM calling functions.

These call the real LLM (Haiku in test mode) to verify the loop
works end-to-end without coupling to Anthropic response internals.
"""

import pytest
from pydantic import BaseModel, Field

from rumil.calls.common import run_agent_loop
from rumil.llm import Tool, text_call, structured_call
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
        max_tokens=64,
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
        max_tokens=256,
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
        max_tokens=256,
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
        max_tokens=128,
        max_rounds=2,
    )
    # max_rounds=2 means at most 3 rounds (0, 1, 2), so tool calls are bounded
    assert len(call_count) <= 4


@pytest.mark.llm
async def test_text_call_returns_string():
    """text_call should return a non-empty string."""
    result = await text_call(
        "You are a helpful assistant.",
        "Say 'yes' and nothing else.",
        max_tokens=16,
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
        max_tokens=256,
    )
    assert result.data is not None
    assert isinstance(result.data["score"], int)
    assert "reason" in result.data
    assert result.input_tokens is not None
    assert result.output_tokens is not None
    assert result.duration_ms is not None
