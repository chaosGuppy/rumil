"""Tests for the generic agent_loop in llm.py.

These call the real LLM (Haiku in test mode) to verify the agent loop
works end-to-end without coupling to Anthropic response internals.
"""

import pytest

from differential.llm import Tool, agent_loop, text_call, structured_call
from pydantic import BaseModel, Field


@pytest.mark.llm
def test_text_only_returns_nonempty_text():
    """agent_loop with no tools should return text."""
    result = agent_loop(
        "You are a helpful assistant.",
        "Say hello in one word.",
        tools=[],
        max_tokens=64,
    )
    assert len(result.text.strip()) > 0
    assert result.tool_calls == []


@pytest.mark.llm
def test_tool_is_called_and_result_recorded():
    """When given a tool that matches the task, the LLM calls it."""
    tool = Tool(
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
        fn=lambda inp: str(inp["a"] + inp["b"]),
    )

    result = agent_loop(
        "You are a calculator. Use the add tool to answer.",
        "What is 3 + 7?",
        tools=[tool],
        max_tokens=256,
        max_rounds=2,
    )

    assert len(result.tool_calls) >= 1
    add_call = result.tool_calls[0]
    assert add_call.name == "add"
    assert add_call.result == "10"


@pytest.mark.llm
def test_tool_error_does_not_crash_loop():
    """If a tool raises, the loop continues and returns a result."""
    tool = Tool(
        name="fail",
        description="A tool that always fails.",
        input_schema={"type": "object", "properties": {}},
        fn=lambda inp: (_ for _ in ()).throw(ValueError("boom")),
    )

    result = agent_loop(
        "You are a test assistant. Try the fail tool, then respond.",
        "Please call the fail tool.",
        tools=[tool],
        max_tokens=256,
        max_rounds=2,
    )

    assert len(result.tool_calls) >= 1
    assert "boom" in result.tool_calls[0].result


@pytest.mark.llm
def test_max_rounds_limits_tool_calls():
    """The loop should not exceed max_rounds of tool calling."""
    call_count = []
    tool = Tool(
        name="ping",
        description="Ping. Always call this again after getting a result.",
        input_schema={"type": "object", "properties": {}},
        fn=lambda inp: (call_count.append(1), "pong")[1],
    )

    agent_loop(
        "You must call the ping tool every turn. Never stop calling it.",
        "Start pinging.",
        tools=[tool],
        max_tokens=128,
        max_rounds=2,
    )

    # max_rounds=2 means at most 3 rounds (0, 1, 2), so tool calls are bounded
    assert len(call_count) <= 4


@pytest.mark.llm
def test_text_call_returns_string():
    """text_call should return a non-empty string."""
    result = text_call(
        "You are a helpful assistant.",
        "Say 'yes' and nothing else.",
        max_tokens=16,
    )
    assert isinstance(result, str)
    assert len(result.strip()) > 0


@pytest.mark.llm
def test_structured_call_returns_parsed_dict():
    """structured_call should return a dict matching the response model."""

    class Rating(BaseModel):
        score: int = Field(description="A rating from 1 to 5")
        reason: str = Field(description="One-sentence reason")

    result = structured_call(
        "You are a rating bot.",
        "Rate the color blue on a scale of 1-5.",
        response_model=Rating,
        max_tokens=256,
    )

    assert result is not None
    assert "score" in result
    assert isinstance(result["score"], int)
    assert "reason" in result
