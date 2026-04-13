"""Generic agent loop for orchestrator runs.

Not worldview-specific — takes a system prompt, tools, user message, and a
tool executor callback. Returns a RunResult with actions taken and final response.
"""

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import anthropic
from anthropic.types import TextBlock, ToolUseBlock

from orchestrator.tracing import RunTracer


def _new_span_id() -> str:
    import uuid

    return uuid.uuid4().hex[:12]


def _serialize_content(content: object) -> list[dict[str, Any]]:
    """Convert Anthropic content blocks to JSON-serializable dicts."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    result = []
    for block in content:  # type: ignore[union-attr]
        if isinstance(block, TextBlock):
            result.append({"type": "text", "text": block.text})
        elif isinstance(block, ToolUseBlock):
            result.append(
                {
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                }
            )
        elif isinstance(block, dict):
            result.append(block)
    return result


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
    tracer: RunTracer | None = None,
) -> RunResult:
    """Run an agent loop: LLM calls tools until it stops or hits max_rounds."""
    client = anthropic.AsyncAnthropic(api_key=api_key)
    messages: list[dict[str, Any]] = [{"role": "user", "content": user_message}]
    result = RunResult()

    root_span = _new_span_id()
    if tracer:
        tracer.span_begin(root_span, "orchestrate_step", f"run_step ({model})")

    for round_num in range(max_rounds):
        round_span = _new_span_id()
        if tracer:
            tracer.span_begin(
                round_span, "round", f"round {round_num + 1}", parent_span_id=root_span
            )

        t0 = time.monotonic()
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
            duration_ms = int((time.monotonic() - t0) * 1000)
            if tracer:
                tracer.record_error(round_span, f"API error: {e}")
                tracer.span_end(round_span)
                tracer.span_end(root_span)
            result.stop_reason = "error"
            result.response = f"API error: {e}"
            result.rounds_used = round_num
            return result
        duration_ms = int((time.monotonic() - t0) * 1000)

        if tracer:
            usage_raw = response.usage
            tracer.record_model_event(
                round_span,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                input_messages=messages,
                tools_offered=list(tools),
                output_content=_serialize_content(response.content),
                stop_reason=response.stop_reason or "unknown",
                usage={
                    "input_tokens": usage_raw.input_tokens,
                    "output_tokens": usage_raw.output_tokens,
                    "cache_read_input_tokens": getattr(
                        usage_raw, "cache_read_input_tokens", 0
                    )
                    or 0,
                    "cache_creation_input_tokens": getattr(
                        usage_raw, "cache_creation_input_tokens", 0
                    )
                    or 0,
                },
                duration_ms=duration_ms,
            )

        text_parts: list[str] = []
        tool_calls: list[ToolUseBlock] = []
        for block in response.content:
            if isinstance(block, TextBlock):
                text_parts.append(block.text)
            elif isinstance(block, ToolUseBlock):
                tool_calls.append(block)

        if not tool_calls:
            if tracer:
                tracer.span_end(round_span)
                tracer.span_end(root_span)
            result.response = "\n".join(text_parts)
            result.rounds_used = round_num + 1
            result.stop_reason = "natural"
            return result

        messages.append({"role": "assistant", "content": response.content})  # type: ignore[arg-type]

        tool_results = []
        for tc in tool_calls:
            t_tool = time.monotonic()
            tool_result = execute_tool(tc.name, tc.input)  # type: ignore[arg-type]
            tool_duration = int((time.monotonic() - t_tool) * 1000)

            if tracer:
                tracer.record_tool_event(
                    round_span,
                    function_name=tc.name,
                    arguments=tc.input,  # type: ignore[arg-type]
                    result=tool_result[:2000],
                    duration_ms=tool_duration,
                )

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
        if tracer:
            tracer.span_end(round_span)

    if tracer:
        tracer.span_end(root_span)
    result.rounds_used = max_rounds
    result.stop_reason = "max_rounds"
    result.response = "Reached max orchestrator rounds."
    return result
