"""Inner-loop runner for axon delegates.

Given a :class:`DelegateConfig`, the (resolved) seed messages, and a
tool list, runs a tool-using agent loop until ``finalize`` is called,
``max_rounds`` is hit, or the budget runs out. Returns the validated
finalize payload + run metadata.

The runner is shared by both regimes:

- *Continuation* (``inherit_context=True``): the orchestrator passes
  the spine's full message stack as ``seed_messages`` plus a single
  framing user message; the inner-loop's first API call hits cache on
  the spine prefix.
- *Isolation* (``inherit_context=False``): the orchestrator passes a
  fresh ``[user_framing]`` as ``seed_messages``; no cache reuse on
  the spine, but the inner loop builds its own cache from there.

This module deliberately doesn't know about the spine — it just runs
a tool-using loop. The orchestrator handles regime selection, cache
prefix construction, and side effects.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import anthropic
from anthropic.types import TextBlock, ToolUseBlock
from anthropic.types.beta import BetaTextBlock, BetaToolUseBlock

from rumil.calls.common import prepare_tools
from rumil.database import DB
from rumil.llm import LLMExchangeMetadata, Tool, ToolCall, call_anthropic_api
from rumil.model_config import ModelConfig
from rumil.orchestrators.axon.budget_clock import BudgetClock
from rumil.orchestrators.axon.tools import FINALIZE_TOOL_NAME
from rumil.settings import get_settings

log = logging.getLogger(__name__)

InnerStopReason = str  # "finalized" | "no_tool_calls" | "max_rounds" | "cost_exhausted"


@dataclass
class InnerLoopResult:
    """What :func:`run_inner_loop` returns to the orchestrator."""

    finalize_payload: dict[str, Any] | None
    """The validated finalize args, or None if the loop terminated some other way."""

    final_text: str
    """The last assistant text emission — useful for force-finalize fallbacks."""

    messages: list[dict]
    """The full mutated message stack — keeps the orchestrator's audit trail."""

    tool_calls: list[ToolCall]
    """All non-finalize tool calls fired during the loop."""

    rounds: int
    """Assistant turns taken (1-indexed)."""

    stopped_because: InnerStopReason


@dataclass
class _FinalizeCapture:
    """Mutable container for the finalize payload, set when the model calls it.

    The finalize tool's fn isn't invoked in the standard fn-dispatch path
    because the orchestrator wants to validate the payload and stop the
    loop instead of returning a tool_result and continuing. We intercept
    by checking tool name in the dispatch loop and reading from this
    container.
    """

    payload: dict[str, Any] | None = None
    captured: bool = False


async def run_inner_loop(
    *,
    system_prompt: str,
    seed_messages: Sequence[dict],
    tools: Sequence[Tool],
    model: str,
    model_config: ModelConfig | None,
    db: DB,
    call_id: str,
    phase: str,
    budget_clock: BudgetClock,
    max_rounds: int,
    cache: bool = True,
    server_tool_defs: Sequence[dict] = (),
) -> InnerLoopResult:
    """Run a tool-using inner loop until finalize / max_rounds / cost_exhausted.

    The expected termination signal is ``finalize``: when the model
    emits a ``finalize`` tool_use, we capture and validate the input
    against the finalize tool's schema (already done by the model since
    the API enforces input_schema for tool_use blocks under strict mode
    — but we re-validate against the caller's schema for safety), and
    then stop. The finalize tool's fn is never invoked.

    Other termination paths:
    - The model emits no tool calls: surface as ``no_tool_calls`` (the
      orchestrator may force-finalize from the last assistant text).
    - ``max_rounds`` reached without a finalize.
    - ``cost_exhausted`` reported by the budget clock.
    """
    if max_rounds < 1:
        raise ValueError(f"max_rounds must be >= 1, got {max_rounds}")
    settings = get_settings()
    client = anthropic.AsyncAnthropic(api_key=settings.require_anthropic_key())
    tool_defs, tool_fns = prepare_tools(tools) if tools else ([], {})
    # Server-tool defs (e.g. anthropic web_search) need to land in the
    # API request's tools list alongside our regular tool defs but no
    # tool_fn dispatch — anthropic resolves them server-side and emits
    # both server_tool_use + result blocks in the same response.
    combined_tool_defs: list[dict] = [*server_tool_defs, *tool_defs]

    messages: list[dict] = list(seed_messages)
    final_text = ""
    all_tool_calls: list[ToolCall] = []
    stopped_because: InnerStopReason = "max_rounds"
    round_idx = 0
    finalize_capture = _FinalizeCapture()

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
            budget_clock.record_exchange(usage, model)

        text_parts: list[str] = []
        tool_uses: list[ToolUseBlock] = []
        for block in response.content:
            if isinstance(block, (TextBlock, BetaTextBlock)):
                text_parts.append(block.text)
            elif isinstance(block, (ToolUseBlock, BetaToolUseBlock)):
                tool_uses.append(block)  # pyright: ignore[reportArgumentType]
        round_text = "\n".join(text_parts)
        final_text = round_text

        messages.append({"role": "assistant", "content": list(response.content)})

        if not tool_uses:
            stopped_because = "no_tool_calls"
            break

        # Pass 1: detect finalize. If present, validate and stop — we
        # do NOT execute peer tool calls in the same turn (the model
        # signaled it's done).
        for tu in tool_uses:
            if tu.name == FINALIZE_TOOL_NAME:
                finalize_capture.payload = dict(tu.input or {})
                finalize_capture.captured = True
                break
        if finalize_capture.captured:
            stopped_because = "finalized"
            break

        # Pass 2: dispatch all peer tool calls.
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
                    log.exception("Inner-loop tool %s raised", tu.name)
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

        budget_block = {
            "type": "text",
            "text": (
                f"[budget] {budget_clock.render_for_prompt()}"
                + (
                    "\n[budget] Cost cap exhausted — finalize your output now."
                    if budget_clock.cost_exhausted
                    else ""
                )
            ),
        }
        messages.append({"role": "user", "content": [*tool_results, budget_block]})

        if budget_clock.cost_exhausted:
            stopped_because = "cost_exhausted"
            break
    else:
        stopped_because = "max_rounds"

    return InnerLoopResult(
        finalize_payload=finalize_capture.payload,
        final_text=final_text,
        messages=messages,
        tool_calls=all_tool_calls,
        rounds=round_idx + 1,
        stopped_because=stopped_because,
    )


def validate_finalize_payload(
    payload: dict[str, Any] | None,
    schema: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    """Best-effort validation of a finalize payload against a JSON Schema.

    We don't carry a full JSON Schema validator here — Anthropic's strict
    mode + the inner loop's tool wiring already enforce the schema at
    the API level. This is a lightweight defensive check for required
    fields and additionalProperties=False before the orchestrator
    surfaces the payload as a delegate's tool_result.

    Returns (validated_payload, error_msg). On success, returns
    (payload, None). On failure, (None, "human-readable explanation").
    """
    if payload is None:
        return None, "finalize payload missing"
    required = schema.get("required") or []
    missing = [k for k in required if k not in payload]
    if missing:
        return None, f"finalize payload missing required field(s): {missing}"
    properties = schema.get("properties") or {}
    if schema.get("additionalProperties") is False:
        unexpected = [k for k in payload if k not in properties]
        if unexpected:
            return None, f"finalize payload has unexpected field(s): {unexpected}"
    return payload, None
