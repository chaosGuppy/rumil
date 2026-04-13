"""Generic agent loop for orchestrator runs.

Not worldview-specific — takes a system prompt, tools, user message, and a
tool executor callback. Returns a RunResult with actions taken and final response.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import anthropic
from anthropic.types import TextBlock, ToolUseBlock


@dataclass
class RunResult:
    actions_taken: list[dict[str, Any]] = field(default_factory=list)
    response: str = ""
    rounds_used: int = 0
    stop_reason: str = "natural"  # "natural" | "max_rounds" | "error"


async def run_step(
    *,
    system_prompt: str,
    user_message: str,
    tools: Sequence[dict[str, Any]],
    execute_tool: Callable[[str, dict[str, Any]], str],
    model: str = "claude-sonnet-4-6",
    max_rounds: int = 8,
    max_tokens: int = 4096,
    temperature: float = 0.5,
    api_key: str | None = None,
    on_action: Callable[[dict[str, Any]], None] | None = None,
) -> RunResult:
    """Run an agent loop: LLM calls tools until it stops or hits max_rounds.

    Args:
        system_prompt: System message for the LLM.
        user_message: Initial user message with context.
        tools: Tool definitions (Anthropic format).
        execute_tool: Callback (tool_name, tool_input) -> result string.
        model: Model identifier.
        max_rounds: Maximum tool-calling rounds.
        max_tokens: Max tokens per LLM response.
        temperature: Sampling temperature.
        api_key: Anthropic API key. If None, uses ANTHROPIC_API_KEY env var.
        on_action: Optional callback fired after each tool execution.

    Returns:
        RunResult with actions taken, final text response, and metadata.
    """
    client = anthropic.AsyncAnthropic(api_key=api_key)
    messages: list[dict[str, Any]] = [{"role": "user", "content": user_message}]
    result = RunResult()

    for round_num in range(max_rounds):
        try:
            response = await client.messages.create(
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                system=system_prompt,
                messages=messages,  # pyright: ignore[reportArgumentType]
                tools=list(tools),  # pyright: ignore[reportArgumentType]
            )
        except Exception as e:
            result.stop_reason = "error"
            result.response = f"API error: {e}"
            result.rounds_used = round_num
            return result

        text_parts: list[str] = []
        tool_calls: list[ToolUseBlock] = []
        for block in response.content:
            if isinstance(block, TextBlock):
                text_parts.append(block.text)
            elif isinstance(block, ToolUseBlock):
                tool_calls.append(block)

        if not tool_calls:
            result.response = "\n".join(text_parts)
            result.rounds_used = round_num + 1
            result.stop_reason = "natural"
            return result

        messages.append({"role": "assistant", "content": response.content})  # type: ignore[arg-type]

        tool_results = []
        for tc in tool_calls:
            tool_result = execute_tool(tc.name, tc.input)  # type: ignore[arg-type]
            action = {"tool": tc.name, "input": tc.input, "result": tool_result[:300]}
            result.actions_taken.append(action)
            if on_action:
                on_action(action)
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tc.id,
                    "content": tool_result,
                }
            )

        messages.append({"role": "user", "content": tool_results})

    result.rounds_used = max_rounds
    result.stop_reason = "max_rounds"
    result.response = "Reached max orchestrator rounds."
    return result
