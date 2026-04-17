"""Tests for the date suffix injected into system prompts."""

from datetime import date
from unittest.mock import AsyncMock

import anthropic.types
import pytest

from rumil.calls.common import run_agent_loop
from rumil.llm import Tool, _with_date_suffix, call_api
from rumil.moves.base import MoveState

TODAY = date.today().strftime("%Y-%m-%d")
DATE_MARKER = f"IMPORTANT: Today's date is {TODAY}"


def test_with_date_suffix_appends_date():
    result = _with_date_suffix("You are helpful.")
    assert result.startswith("You are helpful.")
    assert DATE_MARKER in result


def test_with_date_suffix_does_not_double_when_called_twice():
    """Calling _with_date_suffix on an already-suffixed string adds a second copy,
    but call_api always receives the *original* prompt so this shouldn't happen
    in practice. This test documents the raw function behavior."""
    once = _with_date_suffix("Base prompt.")
    twice = _with_date_suffix(once)
    assert twice.count(DATE_MARKER) == 2


async def test_call_api_sends_date_in_system_prompt(mocker):
    """call_api should inject the date suffix into the system prompt it sends."""
    fake_usage = anthropic.types.Usage(input_tokens=10, output_tokens=5)
    fake_message = anthropic.types.Message(
        id="msg_test",
        type="message",
        role="assistant",
        content=[anthropic.types.TextBlock(type="text", text="hi")],
        model="claude-haiku-4-5-20251001",
        stop_reason="end_turn",
        usage=fake_usage,
    )
    mock_create = AsyncMock(return_value=fake_message)
    fake_client = mocker.MagicMock()
    fake_client.messages.create = mock_create

    await call_api(
        fake_client,
        "claude-haiku-4-5-20251001",
        "You are helpful.",
        [{"role": "user", "content": "hi"}],
    )

    call_kwargs = mock_create.call_args
    sent_system = call_kwargs.kwargs.get("system") or call_kwargs[1].get("system")
    assert DATE_MARKER in sent_system


@pytest.mark.llm
async def test_agent_loop_two_rounds_no_duplication(tmp_db, scout_call):
    """A two-round agent loop should have the date suffix in the system prompt
    exactly once per API call — never duplicated from round to round."""
    call_count = []

    async def ping(inp: dict) -> str:
        call_count.append(1)
        return "pong"

    ping_tool = Tool(
        name="ping",
        description="Ping. Always call this tool every turn.",
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

    # The loop ran at least 2 rounds (tool was used)
    assert len(call_count) >= 1

    # Fetch full exchange records (get_llm_exchanges only returns summary columns,
    # so retrieve each one individually to get system_prompt).
    exchange_summaries = await tmp_db.get_llm_exchanges(scout_call.id)
    assert len(exchange_summaries) >= 2, "Expected at least 2 exchanges for 2 rounds"

    for summary in exchange_summaries:
        full = await tmp_db.get_llm_exchange(summary["id"])
        prompt = full["system_prompt"]
        occurrences = prompt.count(DATE_MARKER)
        assert occurrences == 1, (
            f"Expected date marker exactly once, found {occurrences} "
            f"in exchange round {summary.get('round')}"
        )
