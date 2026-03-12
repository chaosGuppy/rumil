"""
LLM interface. Anthropic-specific details are confined to this module.

Exports three calling modes:
  - agent_loop: generic tool-use conversation loop
  - structured_call: structured output via messages.parse
  - text_call: plain text call
"""

import asyncio
import logging
import time
from collections.abc import Callable, Awaitable
from dataclasses import dataclass, field
from pathlib import Path

import anthropic
from anthropic.types import MessageParam, TextBlock, ToolUseBlock
from pydantic import BaseModel

from differential.settings import get_settings

PROMPTS_DIR = Path(__file__).parent.parent.parent / "prompts"

log = logging.getLogger(__name__)

MAX_API_RETRIES = 4


def _load_file(name: str) -> str:
    path = PROMPTS_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"Prompt file not found: {path}")
    return path.read_text(encoding="utf-8")


def build_system_prompt(call_type: str) -> str:
    """Combine preamble + call-type instructions into one system prompt."""
    preamble = _load_file("preamble.md")
    instructions = _load_file(f"{call_type}.md")
    return f"{preamble}\n\n---\n\n{instructions}"


def build_user_message(context_text: str, task_description: str) -> str:
    """Combine context dump + specific task into one user message."""
    if context_text:
        return f"{context_text}\n\n---\n\n{task_description}"
    return task_description


@dataclass
class Tool:
    """A tool available to the LLM. fn is called with the parsed input dict
    and returns a string result that is sent back as tool_result."""

    name: str
    description: str
    input_schema: dict
    fn: Callable[[dict], Awaitable[str]]


@dataclass
class ToolCall:
    """Record of a single tool call made during agent_loop."""

    name: str
    input: dict
    result: str


@dataclass
class APIResponse:
    """Wrapper around an Anthropic Message with timing info."""

    message: anthropic.types.Message
    duration_ms: int


@dataclass
class RoundRecord:
    """Record of a single API round within an agent_loop."""

    round: int
    response_text: str
    tool_calls: list[ToolCall]
    input_tokens: int
    output_tokens: int
    duration_ms: int = 0
    error: str | None = None


@dataclass
class AgentResult:
    """Result of an agent_loop run."""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    rounds: list[RoundRecord] = field(default_factory=list)
    system_prompt: str = ""
    user_message: str = ""
    warnings: list[str] = field(default_factory=list)


async def _call_api(
    client: anthropic.AsyncAnthropic,
    model: str,
    system_prompt: str,
    messages: list[dict],
    tools: list[dict] | None = None,
    max_tokens: int = 4096,
    warnings: list[str] | None = None,
) -> APIResponse:
    """Make a single Anthropic API call with retry logic."""
    kwargs: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system_prompt,
        "messages": messages,
    }
    if tools:
        kwargs["tools"] = tools

    n_tools = len(tools) if tools else 0
    log.debug(
        "API call: model=%s, max_tokens=%d, tools=%d, system_prompt_len=%d, messages=%d",
        model, max_tokens, n_tools, len(system_prompt), len(messages),
    )

    for attempt in range(MAX_API_RETRIES):
        try:
            start = time.monotonic()
            response = await client.messages.create(**kwargs)
            elapsed_ms = int((time.monotonic() - start) * 1000)
            log.debug(
                "API response: stop_reason=%s, usage=%d/%d tokens, duration=%dms",
                response.stop_reason,
                response.usage.input_tokens,
                response.usage.output_tokens,
                elapsed_ms,
            )
            return APIResponse(message=response, duration_ms=elapsed_ms)
        except Exception as e:
            status = getattr(e, "status_code", None)
            name = type(e).__name__.lower()
            retryable = (
                status in (429, 500, 529)
                or "overloaded" in name
                or "ratelimit" in name
                or "internalserver" in name
                or "overloaded" in str(e).lower()
            )
            if not retryable or attempt == MAX_API_RETRIES - 1:
                log.error("API call failed (non-retryable): %s", e, exc_info=True)
                raise
            wait = 2**attempt
            label = f"HTTP {status}" if status else name
            msg = (
                f"API temporarily unavailable ({label}), "
                f"retrying in {wait}s (attempt {attempt + 1}/{MAX_API_RETRIES})"
            )
            log.warning("%s", msg)
            if warnings is not None:
                warnings.append(msg)
            await asyncio.sleep(wait)

    raise RuntimeError("Unreachable: retry loop exhausted without raising")


async def agent_loop(
    system_prompt: str,
    user_message: str,
    tools: list[Tool],
    *,
    max_tokens: int = 4096,
    max_rounds: int | None = None,
) -> AgentResult:
    """Run a tool-use conversation loop.

    Each Tool's fn is called when the LLM invokes it. The fn's return value
    is sent back as the tool_result content. If fn raises, the exception
    message is sent back as an error result.

    Returns AgentResult with concatenated text and a log of all tool calls.
    """
    settings = get_settings()
    effective_rounds = max_rounds if max_rounds is not None else (
        2 if settings.is_smoke_test else 6
    )
    api_key = settings.require_anthropic_key()
    model = settings.model
    client = anthropic.AsyncAnthropic(api_key=api_key)

    tool_defs = [
        {
            "name": t.name,
            "description": t.description,
            "input_schema": t.input_schema,
        }
        for t in tools
    ]
    tool_fns = {t.name: t.fn for t in tools}

    tool_names = [t.name for t in tools]
    log.debug(
        "agent_loop starting: max_rounds=%d, max_tokens=%d, tools=%s",
        effective_rounds, max_tokens, tool_names,
    )

    messages: list[dict] = [{"role": "user", "content": user_message}]
    text_parts: list[str] = []
    all_tool_calls: list[ToolCall] = []
    all_rounds: list[RoundRecord] = []
    all_warnings: list[str] = []
    round_num = 0

    for round_num in range(effective_rounds + 1):
        log.debug("agent_loop round %d/%d", round_num + 1, effective_rounds)
        api_resp = await _call_api(
            client,
            model,
            system_prompt,
            messages,
            tool_defs or None,
            max_tokens,
            warnings=all_warnings,
        )
        response = api_resp.message

        # Collect text and tool_use blocks
        tool_uses: list[ToolUseBlock] = []
        round_text_parts: list[str] = []
        for block in response.content:
            if isinstance(block, TextBlock):
                text_parts.append(block.text)
                round_text_parts.append(block.text)
            elif isinstance(block, ToolUseBlock):
                tool_uses.append(block)

        round_tool_calls: list[ToolCall] = []

        if response.stop_reason == "end_turn" or not tool_uses:
            log.debug(
                "agent_loop ending: stop_reason=%s, tool_uses=%d, rounds_used=%d",
                response.stop_reason, len(tool_uses), round_num + 1,
            )
            all_rounds.append(RoundRecord(
                round=round_num,
                response_text="\n".join(round_text_parts),
                tool_calls=round_tool_calls,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                duration_ms=api_resp.duration_ms,
            ))
            break

        log.debug(
            "agent_loop round %d: %d tool call(s): %s",
            round_num + 1, len(tool_uses), [tu.name for tu in tool_uses],
        )

        # Call each tool and build tool_result messages
        tool_results: list[dict] = []
        for tu in tool_uses:
            fn = tool_fns.get(tu.name)
            if fn is None:
                result_str = f"Unknown tool: {tu.name}"
                log.warning("Unknown tool called by LLM: %s", tu.name)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": result_str,
                        "is_error": True,
                    }
                )
            else:
                try:
                    result_str = await fn(tu.input)
                except Exception as e:
                    log.error(
                        "Tool %s raised an exception: %s", tu.name, e, exc_info=True,
                    )
                    result_str = f"Error: {e}"
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tu.id,
                            "content": result_str,
                            "is_error": True,
                        }
                    )
                else:
                    log.debug(
                        "Tool %s returned: %s",
                        tu.name, result_str[:200] if result_str else "(empty)",
                    )
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tu.id,
                            "content": result_str,
                        }
                    )
            tc = ToolCall(name=tu.name, input=tu.input, result=result_str)
            all_tool_calls.append(tc)
            round_tool_calls.append(tc)

        all_rounds.append(RoundRecord(
            round=round_num,
            response_text="\n".join(round_text_parts),
            tool_calls=round_tool_calls,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            duration_ms=api_resp.duration_ms,
        ))

        remaining = effective_rounds - round_num
        if remaining == 1:
            budget_note = {
                "type": "text",
                "text": "This is your final round — finish your work now.",
            }
        else:
            budget_note = {
                "type": "text",
                "text": f"After this round of tool calls, you will have {remaining - 1} rounds remaining.",
            }

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results + [budget_note]})

    log.info(
        "agent_loop complete: %d rounds, %d tool calls, %d text chars",
        round_num + 1, len(all_tool_calls), sum(len(t) for t in text_parts),
    )
    return AgentResult(
        text="\n".join(text_parts),
        tool_calls=all_tool_calls,
        rounds=all_rounds,
        system_prompt=system_prompt,
        user_message=user_message,
        warnings=all_warnings,
    )


async def single_call_with_tools(
    system_prompt: str,
    user_message: str,
    tools: list[Tool],
    *,
    max_tokens: int = 4096,
) -> AgentResult:
    """Make a single LLM call with tools. Executes any tool calls the LLM
    makes but does NOT loop back — the results are returned directly.

    Use this when you want the LLM to pick and invoke tools in one shot
    (e.g. phase-1 page loading, single-call prioritization).
    """
    settings = get_settings()
    api_key = settings.require_anthropic_key()
    model = settings.model
    client = anthropic.AsyncAnthropic(api_key=api_key)

    tool_defs = [
        {
            "name": t.name,
            "description": t.description,
            "input_schema": t.input_schema,
        }
        for t in tools
    ]
    tool_fns = {t.name: t.fn for t in tools}

    log.debug(
        "single_call: max_tokens=%d, tools=%s",
        max_tokens, [t.name for t in tools],
    )

    messages: list[dict] = [{"role": "user", "content": user_message}]
    all_warnings: list[str] = []
    api_resp = await _call_api(
        client, model, system_prompt, messages, tool_defs or None, max_tokens,
        warnings=all_warnings,
    )
    response = api_resp.message

    text_parts: list[str] = []
    all_tool_calls: list[ToolCall] = []

    for block in response.content:
        if isinstance(block, TextBlock):
            text_parts.append(block.text)
        elif isinstance(block, ToolUseBlock):
            fn = tool_fns.get(block.name)
            if fn is None:
                result_str = f"Unknown tool: {block.name}"
                log.warning("Unknown tool called by LLM: %s", block.name)
            else:
                try:
                    result_str = await fn(block.input)
                except Exception as e:
                    log.error(
                        "Tool %s raised an exception: %s",
                        block.name, e, exc_info=True,
                    )
                    result_str = f"Error: {e}"
                else:
                    log.debug(
                        "Tool %s returned: %s",
                        block.name, result_str[:200] if result_str else "(empty)",
                    )
            all_tool_calls.append(
                ToolCall(name=block.name, input=block.input, result=result_str)
            )

    round_record = RoundRecord(
        round=0,
        response_text="\n".join(text_parts),
        tool_calls=all_tool_calls,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        duration_ms=api_resp.duration_ms,
    )

    log.info(
        "single_call complete: %d tool calls, %d text chars",
        len(all_tool_calls), sum(len(t) for t in text_parts),
    )
    return AgentResult(
        text="\n".join(text_parts),
        tool_calls=all_tool_calls,
        rounds=[round_record],
        system_prompt=system_prompt,
        user_message=user_message,
        warnings=all_warnings,
    )


async def text_call(
    system_prompt: str,
    user_message: str = "",
    *,
    messages: list[dict] | None = None,
    max_tokens: int = 4096,
) -> str:
    """Make a plain text LLM call. Returns the raw text response.

    Pass `messages` for multi-turn conversations, or `user_message` for single-turn.
    """
    settings = get_settings()
    api_key = settings.require_anthropic_key()
    model = settings.model
    client = anthropic.AsyncAnthropic(api_key=api_key)
    msg_list = (
        messages
        if messages is not None
        else [{"role": "user", "content": user_message}]
    )
    log.debug("text_call: max_tokens=%d, messages=%d", max_tokens, len(msg_list))
    api_resp = await _call_api(client, model, system_prompt, msg_list, max_tokens=max_tokens)
    for block in api_resp.message.content:
        if isinstance(block, TextBlock):
            log.debug("text_call returned %d chars", len(block.text))
            return block.text
    log.debug("text_call returned empty response")
    return ""


async def structured_call(
    system_prompt: str,
    user_message: str,
    response_model: type[BaseModel],
    *,
    max_tokens: int = 1024,
) -> dict | None:
    """Run an LLM call that returns structured output matching response_model.

    Uses the Anthropic structured output API (messages.parse with output_format).
    Returns the parsed response as a dict, or None on failure.
    """
    settings = get_settings()
    api_key = settings.require_anthropic_key()
    model = settings.model
    client = anthropic.AsyncAnthropic(api_key=api_key)

    messages: list[MessageParam] = [{"role": "user", "content": user_message}]
    log.debug(
        "structured_call: model=%s, response_model=%s, max_tokens=%d",
        model, response_model.__name__, max_tokens,
    )

    for attempt in range(MAX_API_RETRIES):
        try:
            response = await client.messages.parse(
                model=model,
                max_tokens=max_tokens,
                system=system_prompt,
                messages=messages,
                output_format=response_model,
            )
            if response.parsed_output is not None:
                log.debug(
                    "structured_call success: %s, usage=%d/%d tokens",
                    response_model.__name__,
                    response.usage.input_tokens,
                    response.usage.output_tokens,
                )
                return response.parsed_output.model_dump()
            log.warning(
                "Structured output was empty (stop_reason=%s, usage=%d/%d tokens)",
                response.stop_reason, response.usage.output_tokens, max_tokens,
            )
            return None
        except Exception as e:
            status = getattr(e, "status_code", None)
            name = type(e).__name__.lower()
            retryable = (
                status in (429, 500, 529)
                or "overloaded" in name
                or "ratelimit" in name
                or "internalserver" in name
                or "overloaded" in str(e).lower()
            )
            if not retryable or attempt == MAX_API_RETRIES - 1:
                log.error(
                    "structured_call failed (non-retryable): %s", e, exc_info=True,
                )
                raise
            wait = 2**attempt
            label = f"HTTP {status}" if status else name
            log.warning(
                "structured_call: API temporarily unavailable (%s), "
                "retrying in %ds (attempt %d/%d)",
                label, wait, attempt + 1, MAX_API_RETRIES,
            )
            await asyncio.sleep(wait)

    raise RuntimeError("Unreachable: retry loop exhausted without raising")
