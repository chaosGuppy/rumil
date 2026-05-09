"""Thin tool-using agent loop shared by mainline + FreeformAgentSubroutine.

Wraps :func:`rumil.llm.call_anthropic_api` to:
- accept a fixed ``model`` (no settings.model dependency)
- run a tool-using loop with arbitrary :class:`rumil.llm.Tool` instances
- record token usage onto a :class:`BudgetClock`
- stop when the model emits no tool calls, hits ``max_rounds``, or the
  budget clock reports tokens_exhausted (caller still drains the final
  response)

Distinct from :func:`rumil.calls.common.run_agent_loop` which requires
a ``MoveState`` and uses ``settings.model``. This one is decoupled from
both — it's a primitive, not a rumil-call participant.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

import anthropic
from anthropic.types import (
    ServerToolUseBlock,
    TextBlock,
    ToolUseBlock,
    WebSearchToolResultBlock,
)
from anthropic.types.beta import (
    BetaServerToolUseBlock,
    BetaTextBlock,
    BetaToolUseBlock,
    BetaWebSearchToolResultBlock,
)

from rumil.calls.common import prepare_tools
from rumil.database import DB
from rumil.llm import LLMExchangeMetadata, Tool, ToolCall, call_anthropic_api
from rumil.model_config import ModelConfig
from rumil.orchestrators.simple_spine.budget_clock import BudgetClock
from rumil.settings import get_settings

log = logging.getLogger(__name__)


def strip_orphaned_server_tool_uses(content: Sequence[Any]) -> list[Any]:
    """Drop server_tool_use blocks that lack a matching tool_result.

    Anthropic server tools (e.g. ``web_search``) execute server-side and
    normally land paired blocks in the same assistant response —
    ``server_tool_use`` followed by its ``*_tool_result``. When the
    response is cut short (max_tokens hit mid-tool, transient API
    failure), the result block can be missing. Sending the unpaired
    ``server_tool_use`` back on the next turn makes the API reject the
    request with ``server_tool_use ... was found without a corresponding
    *_tool_result block``.

    Strip orphaned ``server_tool_use`` blocks so the assistant message
    we re-send is well-formed. Logs each drop — frequent drops are a
    signal that ``max_tokens`` is too small for the surfaced result.
    """
    server_use_blocks: list[tuple[int, Any]] = []
    matched_result_ids: set[str] = set()
    for i, block in enumerate(content):
        if isinstance(block, (ServerToolUseBlock, BetaServerToolUseBlock)):
            server_use_blocks.append((i, block))
        elif isinstance(block, (WebSearchToolResultBlock, BetaWebSearchToolResultBlock)):
            matched_result_ids.add(block.tool_use_id)
    drop_indexes: set[int] = set()
    for i, block in server_use_blocks:
        if block.id not in matched_result_ids:
            log.warning(
                "Dropping orphaned server_tool_use id=%s name=%s — no matching "
                "tool_result block in this response (likely max_tokens cutoff).",
                block.id,
                getattr(block, "name", "?"),
            )
            drop_indexes.add(i)
    if not drop_indexes:
        return list(content)
    return [b for i, b in enumerate(content) if i not in drop_indexes]


@dataclass
class ThinLoopResult:
    final_text: str
    messages: list[dict]
    tool_calls: list[ToolCall]
    rounds: int
    stopped_because: str  # "no_tool_calls" | "max_rounds" | "tokens_exhausted"


async def thin_agent_loop(
    *,
    system_prompt: str,
    messages: list[dict],
    tools: Sequence[Tool],
    model: str,
    model_config: ModelConfig | None,
    db: DB,
    call_id: str,
    phase: str,
    budget_clock: BudgetClock,
    max_rounds: int,
    cache: bool = True,
    on_assistant_message: Callable[[int, str, list[ToolUseBlock]], Awaitable[None]] | None = None,
    server_tool_defs: Sequence[dict] = (),
) -> ThinLoopResult:
    """Run the tool-using loop until termination.

    ``messages`` is mutated in place — the caller can keep a reference
    and observe the appended assistant + tool_result turns. Each
    assistant message lands as one element; each batch of tool_results
    lands as one user-role element grouping all results from that round.

    ``server_tool_defs`` are raw Anthropic-side tool dicts
    (e.g. ``{"type": "web_search_20250305", "name": "web_search", ...}``)
    that Anthropic executes server-side inside a single assistant turn —
    their ``server_tool_use`` + result blocks land in the same response's
    ``content`` and need no follow-up tool_result. We just splice them
    onto the API call's ``tools`` arg.
    """
    if max_rounds < 1:
        raise ValueError(f"max_rounds must be >= 1, got {max_rounds}")
    settings = get_settings()
    client = anthropic.AsyncAnthropic(api_key=settings.require_anthropic_key())
    tool_defs, tool_fns = prepare_tools(tools) if tools else ([], {})
    combined_tool_defs: list[dict] = [*server_tool_defs, *tool_defs]

    final_text = ""
    all_tool_calls: list[ToolCall] = []
    stopped_because = "max_rounds"
    round_idx = 0

    for round_idx in range(max_rounds):
        meta = LLMExchangeMetadata(
            call_id=call_id,
            phase=phase,
            round_num=round_idx,
        )
        api_resp = await call_anthropic_api(
            client,
            model,
            system_prompt,
            messages,
            combined_tool_defs or None,
            metadata=meta,
            db=db,
            cache=cache,
            model_config=model_config,
        )
        response = api_resp.message
        usage = response.usage
        if usage is not None:
            total = (usage.input_tokens or 0) + (usage.output_tokens or 0)
            budget_clock.record_tokens(total)

        text_parts: list[str] = []
        tool_uses: list[ToolUseBlock] = []
        for block in response.content:
            if isinstance(block, (TextBlock, BetaTextBlock)):
                text_parts.append(block.text)
            elif isinstance(block, (ToolUseBlock, BetaToolUseBlock)):
                tool_uses.append(block)  # pyright: ignore[reportArgumentType]
        round_text = "\n".join(text_parts)
        final_text = round_text

        # Append the assistant message preserving thinking + tool_use
        # blocks. Sanitize first: orphaned server_tool_use blocks (no
        # matching tool_result, e.g. from max_tokens mid-execution) are
        # dropped so the next API turn isn't rejected for unpaired ids.
        messages.append(
            {
                "role": "assistant",
                "content": strip_orphaned_server_tool_uses(response.content),
            }
        )

        if on_assistant_message is not None:
            await on_assistant_message(round_idx, round_text, tool_uses)

        if not tool_uses:
            stopped_because = "no_tool_calls"
            break

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
                    result_str = await fn(tu.input)
                except Exception as e:
                    log.exception("Tool %s raised", tu.name)
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
            all_tool_calls.append(ToolCall(name=tu.name, input=tu.input, result=result_str))
        # Mirror mainline's pattern (orchestrator.py): append a budget
        # telemetry block alongside tool_results so the spawn agent sees
        # up-to-date counters each round and can wind down before the
        # cap is hit. Single-round spawns (max_rounds=1) never reach
        # this point because they break out on `no_tool_calls` first.
        budget_block = {
            "type": "text",
            "text": (
                f"[budget] {budget_clock.render_for_prompt()}"
                + (
                    "\n[budget] Token cap exhausted — produce your final output now."
                    if budget_clock.tokens_exhausted
                    else ""
                )
            ),
        }
        messages.append({"role": "user", "content": [*tool_results, budget_block]})

        if budget_clock.tokens_exhausted:
            stopped_because = "tokens_exhausted"
            break
    else:
        stopped_because = "max_rounds"

    return ThinLoopResult(
        final_text=final_text,
        messages=messages,
        tool_calls=all_tool_calls,
        rounds=round_idx + 1,
        stopped_because=stopped_because,
    )
