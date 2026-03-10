"""
LLM interface. Anthropic-specific details are confined to this module.

Exports three calling modes:
  - agent_loop: generic tool-use conversation loop
  - structured_call: structured output via messages.parse
  - text_call: plain text call
"""

import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import anthropic
from anthropic.types import MessageParam, TextBlock, ToolUseBlock
from pydantic import BaseModel

PROMPTS_DIR = Path(__file__).parent.parent.parent / "prompts"
MODEL = (
    "claude-haiku-4-5-20251001"
    if os.environ.get("DIFFERENTIAL_TEST_MODE")
    else "claude-opus-4-6"
)

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
    fn: Callable[[dict], str]


@dataclass
class ToolCall:
    """Record of a single tool call made during agent_loop."""

    name: str
    input: dict
    result: str


@dataclass
class AgentResult:
    """Result of an agent_loop run."""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)


def _call_api(
    client: anthropic.Anthropic,
    system_prompt: str,
    messages: list[dict],
    tools: list[dict] | None = None,
    max_tokens: int = 4096,
) -> anthropic.types.Message:
    """Make a single Anthropic API call with retry logic."""
    kwargs: dict = {
        "model": MODEL,
        "max_tokens": max_tokens,
        "system": system_prompt,
        "messages": messages,
    }
    if tools:
        kwargs["tools"] = tools

    for attempt in range(MAX_API_RETRIES):
        try:
            return client.messages.create(**kwargs)
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
                raise
            wait = 2**attempt
            label = f"HTTP {status}" if status else name
            print(
                f"  [llm] API temporarily unavailable ({label}), "
                f"retrying in {wait}s... (attempt {attempt + 1}/{MAX_API_RETRIES})"
            )
            time.sleep(wait)

    raise RuntimeError("Unreachable: retry loop exhausted without raising")


def agent_loop(
    system_prompt: str,
    user_message: str,
    tools: list[Tool],
    *,
    max_tokens: int = 4096,
    max_rounds: int = 6,
) -> AgentResult:
    """Run a tool-use conversation loop.

    Each Tool's fn is called when the LLM invokes it. The fn's return value
    is sent back as the tool_result content. If fn raises, the exception
    message is sent back as an error result.

    Returns AgentResult with concatenated text and a log of all tool calls.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError(
            "ANTHROPIC_API_KEY environment variable not set. "
            "Set it before running the workspace."
        )
    client = anthropic.Anthropic(api_key=api_key)

    tool_defs = [
        {
            "name": t.name,
            "description": t.description,
            "input_schema": t.input_schema,
        }
        for t in tools
    ]
    tool_fns = {t.name: t.fn for t in tools}

    messages: list[dict] = [{"role": "user", "content": user_message}]
    text_parts: list[str] = []
    all_tool_calls: list[ToolCall] = []

    for round_num in range(max_rounds + 1):
        response = _call_api(
            client,
            system_prompt,
            messages,
            tool_defs or None,
            max_tokens,
        )

        # Collect text and tool_use blocks
        tool_uses: list[ToolUseBlock] = []
        for block in response.content:
            if isinstance(block, TextBlock):
                text_parts.append(block.text)
            elif isinstance(block, ToolUseBlock):
                tool_uses.append(block)

        if response.stop_reason == "end_turn" or not tool_uses:
            break

        # Call each tool and build tool_result messages
        tool_results: list[dict] = []
        for tu in tool_uses:
            fn = tool_fns.get(tu.name)
            if fn is None:
                result_str = f"Unknown tool: {tu.name}"
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
                    result_str = fn(tu.input)
                except Exception as e:
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
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tu.id,
                            "content": result_str,
                        }
                    )
            all_tool_calls.append(
                ToolCall(
                    name=tu.name,
                    input=tu.input,
                    result=result_str,
                )
            )

        remaining = max_rounds - round_num
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

    return AgentResult(
        text="\n".join(text_parts),
        tool_calls=all_tool_calls,
    )


def text_call(
    system_prompt: str,
    user_message: str = "",
    *,
    messages: list[dict] | None = None,
    max_tokens: int = 4096,
) -> str:
    """Make a plain text LLM call. Returns the raw text response.

    Pass `messages` for multi-turn conversations, or `user_message` for single-turn.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY environment variable not set.")
    client = anthropic.Anthropic(api_key=api_key)
    msg_list = (
        messages
        if messages is not None
        else [{"role": "user", "content": user_message}]
    )
    response = _call_api(client, system_prompt, msg_list, max_tokens=max_tokens)
    for block in response.content:
        if isinstance(block, TextBlock):
            return block.text
    return ""


def structured_call(
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
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY environment variable not set.")
    client = anthropic.Anthropic(api_key=api_key)

    messages: list[MessageParam] = [{"role": "user", "content": user_message}]

    for attempt in range(MAX_API_RETRIES):
        try:
            response = client.messages.parse(
                model=MODEL,
                max_tokens=max_tokens,
                system=system_prompt,
                messages=messages,
                output_format=response_model,
            )
            if response.parsed_output is not None:
                return response.parsed_output.model_dump()
            print(
                f"  [llm] Structured output was empty "
                f"(stop_reason={response.stop_reason}, "
                f"usage={response.usage.output_tokens}/{max_tokens} tokens)"
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
                raise
            wait = 2**attempt
            label = f"HTTP {status}" if status else name
            print(
                f"  [llm] API temporarily unavailable ({label}), "
                f"retrying in {wait}s... (attempt {attempt + 1}/{MAX_API_RETRIES})"
            )
            time.sleep(wait)

    raise RuntimeError("Unreachable: retry loop exhausted without raising")
