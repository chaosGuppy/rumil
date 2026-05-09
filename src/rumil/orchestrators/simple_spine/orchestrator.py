"""SimpleSpineOrchestrator — structured-rounds main loop with parallel spawns.

Runs the mainline agent on a persistent thread:

- One assistant turn per "round". Each turn may include reasoning text
  + ``finalize`` + N parallel spawn tool calls.
- Spawn tool calls in the same turn are executed concurrently via
  ``asyncio.gather``; each spawn does its own internal config-prep step
  (when its ``SubroutineDef`` declares one) before running.
- Token budget is the only hard cap. Wall-clock + soft round count are
  surfaced in the per-round system reminder; the agent self-paces.
- On token exhaustion, the next round's prompt instructs the agent to
  finalize. If it still emits no ``finalize`` tool, the orch synthesizes
  a finalize from the last assistant text (``last_status='incomplete'``).

The orchestrator is independent of versus — it returns an :class:`OrchResult`
the caller decides what to do with. The versus :class:`SimpleSpineWorkflow`
adapter wraps this and writes ``answer_text`` to ``question.content``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

import anthropic
from anthropic.types import TextBlock, ToolUseBlock
from pydantic import BaseModel, ValidationError

from rumil.calls.common import mark_call_completed
from rumil.database import DB
from rumil.llm import LLMExchangeMetadata, Tool, call_anthropic_api, structured_call
from rumil.model_config import ModelConfig
from rumil.models import Call, CallStatus, CallType
from rumil.orchestrators.simple_spine.budget_clock import BudgetClock
from rumil.orchestrators.simple_spine.config import (
    OrchInputs,
    OrchResult,
    SimpleSpineConfig,
)
from rumil.orchestrators.simple_spine.subroutines.base import (
    SpawnCtx,
    SubroutineDef,
    SubroutineResult,
    splice_assumptions,
)
from rumil.orchestrators.simple_spine.tools import make_finalize_tool
from rumil.orchestrators.simple_spine.trace_events import (
    SpineConfigPrepEvent,
    SpineFinalizedEvent,
    SpineRoundStartedEvent,
    SpineSpawnCompletedEvent,
    SpineSpawnStartedEvent,
    SpineThrottledEvent,
)
from rumil.settings import get_settings
from rumil.tracing.broadcast import Broadcaster
from rumil.tracing.tracer import CallTrace, reset_trace, set_trace

log = logging.getLogger(__name__)

# Hard cap on assistant turns, regardless of soft max_rounds. Prevents
# infinite loops if the model never finalizes and never runs out of tokens.
_HARD_MAX_ROUNDS = 50


class SimpleSpineOrchestrator:
    """SimpleSpine main-loop orchestrator. See module docstring."""

    def __init__(
        self,
        db: DB,
        config: SimpleSpineConfig,
        broadcaster: Broadcaster | None = None,
    ) -> None:
        self.db = db
        self.config = config
        self.broadcaster = broadcaster

    async def run(
        self,
        inputs: OrchInputs,
        *,
        call_type: CallType = CallType.CLAUDE_CODE_DIRECT,
        parent_call_id: str | None = None,
        budget_clock: BudgetClock | None = None,
    ) -> OrchResult:
        """Run one SimpleSpine cycle and return the result.

        ``call_type`` is the rumil ``CallType`` recorded on the orch's
        own Call row. Versus uses ``VERSUS_COMPLETE`` / ``VERSUS_JUDGE``;
        non-versus invocations can use ``CLAUDE_CODE_DIRECT`` or any
        other type that makes sense for the trace UI. Passing a
        ``budget_clock`` lets a parent SimpleSpine carve a sub-clock
        (``carve_child``) so token spend rolls up — the child orch's
        own ``inputs.budget`` is then ignored.
        """
        clock = budget_clock or BudgetClock(spec=inputs.budget)

        call = await self.db.create_call(
            call_type=call_type,
            scope_page_id=inputs.question_id,
            parent_call_id=parent_call_id,
            call_params={
                "orchestrator": "simple_spine",
                "fingerprint": self.config.fingerprint_short,
                "fingerprint_full": self.config.fingerprint,
                "max_tokens": clock.spec.max_tokens,
                "wall_clock_soft_s": clock.spec.wall_clock_soft_s,
                "max_rounds_soft": clock.spec.max_rounds_soft,
                "library": [s.name for s in self.config.process_library],
            },
        )
        await self.db.update_call_status(call.id, CallStatus.RUNNING)
        trace = CallTrace(call.id, self.db, broadcaster=self.broadcaster)
        trace_token = set_trace(trace)
        try:
            return await self._run_inner(call, inputs, clock, trace)
        finally:
            reset_trace(trace_token)

    async def _run_inner(
        self,
        call: Call,
        inputs: OrchInputs,
        clock: BudgetClock,
        trace: CallTrace,
    ) -> OrchResult:
        question = await self.db.get_page(inputs.question_id)
        if question is None:
            raise RuntimeError(f"SimpleSpine: question {inputs.question_id} not found")

        initial_user = _build_initial_user_message(
            question_id=inputs.question_id,
            question_headline=question.headline,
            question_content=question.content,
            inputs=inputs,
            clock=clock,
        )
        messages: list[dict] = [{"role": "user", "content": initial_user}]

        finalize_state: dict[str, Any] = {
            "done": False,
            "answer": "",
            "reason": "",
            "synthesized": False,
        }

        async def on_finalize(answer: str) -> str:
            finalize_state["done"] = True
            finalize_state["answer"] = answer
            return "Finalized. The harness will return your answer."

        spawn_tools = self._build_spawn_tools(call.id, inputs.question_id, clock)
        all_tools: list[Tool] = list(spawn_tools)
        if self.config.enable_finalize_tool:
            all_tools.append(make_finalize_tool(on_finalize))

        client = anthropic.AsyncAnthropic(api_key=get_settings().require_anthropic_key())
        tool_defs = [
            {"name": t.name, "description": t.description, "input_schema": t.input_schema}
            for t in all_tools
        ]
        tool_fn_map = {t.name: t.fn for t in all_tools}
        effective_system_prompt = splice_assumptions(
            self.config.main_system_prompt, inputs.operating_assumptions
        )

        last_status = "complete"
        spawn_count = 0
        finalize_reason = ""

        for round_idx in range(_HARD_MAX_ROUNDS):
            await trace.record(
                SpineRoundStartedEvent(
                    round_idx=round_idx,
                    tokens_used=clock.tokens_used,
                    tokens_remaining=clock.tokens_remaining,
                    elapsed_s=clock.elapsed_s,
                )
            )
            if clock.tokens_exhausted:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "[system] Token budget exhausted. Call `finalize` "
                            "now with the best answer you can synthesize from "
                            "the work so far. No further spawns will run."
                        ),
                    }
                )

            # Bumped from 8192 → 32k so mainline can carry a long
            # `finalize.answer` (the deliverable lives in the tool
            # input). At 8192 we'd silently truncate any deliverable
            # over ~32k chars; matches the drafter cap so any draft a
            # subroutine can produce, mainline can pass through.
            cfg = ModelConfig(temperature=1.0, max_tokens=32_000)
            api_resp = await call_anthropic_api(
                client,
                self.config.main_model,
                effective_system_prompt,
                messages,
                tool_defs,
                metadata=LLMExchangeMetadata(
                    call_id=call.id,
                    phase="mainline",
                    round_num=round_idx,
                ),
                db=self.db,
                cache=True,
                model_config=cfg,
            )
            response = api_resp.message
            usage = response.usage
            if usage is not None:
                clock.record_tokens((usage.input_tokens or 0) + (usage.output_tokens or 0))
            clock.record_round()

            assistant_text = ""
            tool_uses: list[ToolUseBlock] = []
            for block in response.content:
                if isinstance(block, TextBlock):
                    assistant_text += block.text
                elif isinstance(block, ToolUseBlock):
                    tool_uses.append(block)
            messages.append({"role": "assistant", "content": response.content})

            if not tool_uses:
                if clock.tokens_exhausted and self.config.force_finalize_on_token_exhaustion:
                    finalize_state["answer"] = assistant_text.strip()
                    finalize_state["synthesized"] = True
                    finalize_reason = "token_exhaustion_synthesized"
                    last_status = "incomplete"
                    break
                # A text-only turn isn't a termination signal — the model
                # may be planning aloud before the next batch of spawns.
                # Nudge it and loop again. Token cap + _HARD_MAX_ROUNDS
                # bound the worst case if the model never emits tools.
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "[system] No tools called this turn. Spawn "
                            "subroutines, call `finalize` with your "
                            "deliverable, or note what you're waiting on.\n"
                            f"[budget] {clock.render_for_prompt()}"
                        ),
                    }
                )
                continue

            tool_results: list[dict] = []
            spawn_uses: list[ToolUseBlock] = []
            for tu in tool_uses:
                if tu.name == "finalize":
                    fn = tool_fn_map[tu.name]
                    try:
                        result_str = await fn(tu.input)
                    except Exception as e:
                        result_str = f"Error: {e}"
                        log.exception("Tool %s raised", tu.name)
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tu.id,
                            "content": result_str,
                        }
                    )
                    if tu.name == "finalize":
                        finalize_reason = str(tu.input.get("reason", "")) or "model_finalize"
                else:
                    spawn_uses.append(tu)

            kept_spawn_uses = spawn_uses
            cap = self.config.max_parallel_spawns_per_turn
            if cap is not None and len(spawn_uses) > cap:
                kept_spawn_uses = spawn_uses[:cap]
                throttled = spawn_uses[cap:]
                await trace.record(
                    SpineThrottledEvent(
                        round_idx=round_idx,
                        requested=len(spawn_uses),
                        kept=len(kept_spawn_uses),
                    )
                )
                for tu in throttled:
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tu.id,
                            "content": (
                                f"[throttled] You requested {len(spawn_uses)} "
                                f"parallel spawns; only {cap} were run this "
                                "turn. Try the rest next turn if still wanted."
                            ),
                            "is_error": True,
                        }
                    )

            if kept_spawn_uses and not clock.tokens_exhausted:
                spawn_results = await asyncio.gather(
                    *[
                        self._run_spawn(
                            tu,
                            call_id=call.id,
                            question_id=inputs.question_id,
                            clock=clock,
                            mainline_messages=messages,
                            round_idx=round_idx,
                            trace=trace,
                            operating_assumptions=inputs.operating_assumptions,
                        )
                        for tu in kept_spawn_uses
                    ],
                    return_exceptions=True,
                )
                spawn_count += len(kept_spawn_uses)
                for tu, res in zip(kept_spawn_uses, spawn_results):
                    if isinstance(res, BaseException):
                        log.exception("Spawn %s raised", tu.name, exc_info=res)
                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": tu.id,
                                "content": f"Spawn error: {type(res).__name__}: {res}",
                                "is_error": True,
                            }
                        )
                    else:
                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": tu.id,
                                "content": res.text_summary,
                            }
                        )
            elif kept_spawn_uses and clock.tokens_exhausted:
                for tu in kept_spawn_uses:
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tu.id,
                            "content": (
                                "[budget] Token budget exhausted before this "
                                "spawn could run. Finalize next turn."
                            ),
                            "is_error": True,
                        }
                    )

            if tool_results:
                # Budget telemetry rides along on the tool-result message
                # so the model sees up-to-date counters every turn without
                # us having to insert a separate user turn (which would
                # break the tool_use → tool_result adjacency rule).
                budget_block = {
                    "type": "text",
                    "text": (
                        f"[budget] {clock.render_for_prompt()}"
                        + (
                            "\n[budget] Token cap exhausted — finalize on your next turn."
                            if clock.tokens_exhausted
                            else ""
                        )
                    ),
                }
                messages.append({"role": "user", "content": [*tool_results, budget_block]})

            if finalize_state["done"]:
                break
        else:
            last_status = "incomplete"
            finalize_reason = "hard_round_cap"

        await trace.record(
            SpineFinalizedEvent(
                round_idx=clock.rounds_used,
                answer_chars=len(finalize_state["answer"]),
                reason=finalize_reason,
                synthesized=bool(finalize_state["synthesized"]),
            )
        )

        structured_answer: BaseModel | None = None
        if inputs.output_schema is not None and finalize_state["answer"]:
            structured_answer = await _validate_finalize(
                finalize_state["answer"],
                inputs.output_schema,
                model=self.config.main_model,
                call_id=call.id,
                db=self.db,
            )

        await mark_call_completed(
            call,
            self.db,
            summary=(
                f"simple_spine: {last_status} "
                f"(rounds={clock.rounds_used}, tokens={clock.tokens_used}, "
                f"spawns={spawn_count})"
            ),
        )

        return OrchResult(
            answer_text=finalize_state["answer"],
            structured_answer=structured_answer,
            fingerprint=self.config.fingerprint,
            call_id=call.id,
            spawn_count=spawn_count,
            rounds=clock.rounds_used,
            tokens_used=clock.tokens_used,
            elapsed_s=clock.elapsed_s,
            finalize_reason=finalize_reason,
            last_status=last_status,
        )

    def _build_spawn_tools(
        self,
        call_id: str,
        question_id: str,
        clock: BudgetClock,
    ) -> list[Tool]:
        out: list[Tool] = []
        for sub in self.config.process_library:
            out.append(self._make_spawn_tool(sub, call_id, question_id, clock))
        return out

    def _make_spawn_tool(
        self,
        sub: SubroutineDef,
        call_id: str,
        question_id: str,
        clock: BudgetClock,
    ) -> Tool:
        async def fn(args: dict) -> str:
            # The fn here is invoked by the standard tool-execution path
            # (used in some layers); the orchestrator's main loop
            # bypasses it via _run_spawn. Kept for completeness so the
            # Tool object is fully self-contained.
            spawn_id = str(uuid.uuid4())
            ctx = SpawnCtx(
                db=self.db,
                budget_clock=clock,
                broadcaster=self.broadcaster,
                parent_call_id=call_id,
                question_id=question_id,
                spawn_id=spawn_id,
            )
            res = await sub.run(ctx, args)
            return res.text_summary

        return Tool(
            name=f"spawn_{sub.name}",
            description=_format_tool_description(sub),
            input_schema=sub.spawn_tool_schema(),
            fn=fn,
        )

    async def _run_spawn(
        self,
        tu: ToolUseBlock,
        *,
        call_id: str,
        question_id: str,
        clock: BudgetClock,
        mainline_messages: Sequence[Mapping[str, Any]],
        round_idx: int,
        trace: CallTrace,
        operating_assumptions: str = "",
    ) -> SubroutineResult:
        """Resolve the spawn tool by name → SubroutineDef and run it.

        Bypasses the Tool.fn path so we can record per-spawn trace events
        and run the optional config-prep call before delegating to
        ``SubroutineDef.run``.
        """
        if not tu.name.startswith("spawn_"):
            raise RuntimeError(f"_run_spawn called with non-spawn tool {tu.name}")
        sub_name = tu.name.removeprefix("spawn_")
        sub = next(
            (s for s in self.config.process_library if s.name == sub_name),
            None,
        )
        if sub is None:
            raise KeyError(f"Unknown subroutine: {sub_name}")
        spawn_id = str(uuid.uuid4())
        await trace.record(
            SpineSpawnStartedEvent(
                round_idx=round_idx,
                spawn_id=spawn_id,
                subroutine_name=sub.name,
                overrides=dict(tu.input),
            )
        )
        ctx = SpawnCtx(
            db=self.db,
            budget_clock=clock,
            broadcaster=self.broadcaster,
            parent_call_id=call_id,
            question_id=question_id,
            spawn_id=spawn_id,
            operating_assumptions=operating_assumptions,
        )
        if sub.config_prep is not None:
            ctx.prepped_config = await self._run_config_prep(
                sub,
                ctx,
                tu.input,
                call_id=call_id,
                mainline_messages=mainline_messages,
                round_idx=round_idx,
                trace=trace,
            )
        tokens_before = clock.tokens_used
        try:
            result = await sub.run(ctx, tu.input)
        except Exception as e:
            await trace.record(
                SpineSpawnCompletedEvent(
                    round_idx=round_idx,
                    spawn_id=spawn_id,
                    subroutine_name=sub.name,
                    text_summary_chars=0,
                    error=f"{type(e).__name__}: {e}",
                )
            )
            raise
        tokens_consumed = max(clock.tokens_used - tokens_before, 0)
        result = SubroutineResult(
            text_summary=f"[spawn cost: {tokens_consumed:,} tokens]\n{result.text_summary}",
            tokens_used=result.tokens_used,
            extra={**dict(result.extra), "tokens_consumed": tokens_consumed},
        )
        await trace.record(
            SpineSpawnCompletedEvent(
                round_idx=round_idx,
                spawn_id=spawn_id,
                subroutine_name=sub.name,
                text_summary_chars=len(result.text_summary),
                extra=dict(result.extra),
            )
        )
        return result

    async def _run_config_prep(
        self,
        sub: SubroutineDef,
        ctx: SpawnCtx,
        intent_payload: Mapping[str, Any],
        *,
        call_id: str,
        mainline_messages: Sequence[Mapping[str, Any]],
        round_idx: int,
        trace: CallTrace,
    ) -> BaseModel | None:
        prep = sub.config_prep
        assert prep is not None
        slice_msgs: Sequence[Mapping[str, Any]] = []
        if prep.mainline_context == "last_turn":
            slice_msgs = mainline_messages[-2:] if mainline_messages else []
        elif prep.mainline_context == "last_k_turns":
            k = max(prep.last_k * 2, 0)
            slice_msgs = mainline_messages[-k:] if k else []
        prep_user = json.dumps(
            {
                "subroutine_name": sub.name,
                "intent_payload": dict(intent_payload),
                "mainline_thread_excerpt": _summarize_messages(slice_msgs),
            },
            ensure_ascii=False,
            indent=2,
        )
        prepped_result = await structured_call(
            prep.sys_prompt,
            prep_user,
            prep.output_schema,
            metadata=LLMExchangeMetadata(
                call_id=call_id,
                phase=f"config_prep:{sub.name}",
                round_num=round_idx,
            ),
            db=self.db,
            model=prep.model,
        )
        prepped = prepped_result.parsed
        if prepped is None:
            log.warning(
                "SimpleSpine: config_prep for %s returned no parsed object; "
                "subroutine will run with static defaults",
                sub.name,
            )
            return None
        await trace.record(
            SpineConfigPrepEvent(
                round_idx=round_idx,
                spawn_id=ctx.spawn_id,
                subroutine_name=sub.name,
                prepped_config=prepped.model_dump(),
            )
        )
        return prepped


def _format_tool_description(sub: SubroutineDef) -> str:
    """Compose the spawn tool description shown to mainline.

    Layered on top of the author-supplied ``sub.description``:
    - a ``2-step`` annotation when ``config_prep`` is set, so mainline
      knows there's a hidden elaboration call between its ``intent`` and
      the agent's actual prompt (and that the elaborator can see a slice
      of the mainline thread);
    - an author-supplied ``cost_hint`` line so mainline can plan its
      first spawn before live ``[spawn cost: …]`` feedback arrives.
    """
    parts: list[str] = [sub.description.rstrip()]
    if sub.config_prep is not None:
        scope = sub.config_prep.mainline_context
        parts.append(
            f"_(2-step: thin intent → elaborator fills sys/user/tools; "
            f"elaborator sees mainline_context={scope})_"
        )
    cost_hint = getattr(sub, "cost_hint", None)
    if cost_hint:
        parts.append(f"Cost hint: {cost_hint}")
    return "\n\n".join(parts)


def _summarize_messages(messages: Sequence[Mapping[str, Any]]) -> list[dict]:
    """Compact representation of a slice of mainline thread for prep call."""
    out: list[dict] = []
    for m in messages:
        role = m.get("role", "?")
        content = m.get("content", "")
        if isinstance(content, str):
            out.append({"role": role, "text": content[:2000]})
        else:
            # Anthropic content blocks; flatten text + tool calls.
            parts: list[str] = []
            for b in content:  # type: ignore[assignment]
                if isinstance(b, dict):
                    bt = b.get("type", "")
                    if bt == "text":
                        parts.append(str(b.get("text", "")))
                    elif bt == "tool_use":
                        parts.append(f"[tool_use:{b.get('name', '')}]")
                    elif bt == "tool_result":
                        parts.append(f"[tool_result:{str(b.get('content', ''))[:400]}]")
                else:
                    parts.append(str(b)[:400])
            out.append({"role": role, "text": "\n".join(parts)[:2000]})
    return out


def _build_initial_user_message(
    *,
    question_id: str,
    question_headline: str,
    question_content: str,
    inputs: OrchInputs,
    clock: BudgetClock,
) -> str:
    sections: list[str] = [
        "## Scope question",
        f"`{question_id}` — {question_headline}",
        "",
        question_content.strip(),
        "",
    ]
    if inputs.additional_context.strip():
        sections.append("## Additional context (from caller)")
        sections.append(inputs.additional_context.strip())
        sections.append("")
    if inputs.output_guidance.strip():
        sections.append("## Output guidance")
        sections.append(inputs.output_guidance.strip())
        sections.append("")
    if inputs.output_schema is not None:
        sections.append("## Output schema (your finalize answer will be parsed against this)")
        sections.append(
            f"```json\n{json.dumps(inputs.output_schema.model_json_schema(), indent=2)}\n```"
        )
        sections.append("")
    sections.append("## Budget status")
    sections.append(clock.render_for_prompt())
    sections.append("")
    sections.append(
        "Begin. Plan your first round of spawns or note your initial reading; "
        "call `finalize` when you have a deliverable."
    )
    return "\n".join(sections)


async def _validate_finalize(
    answer_text: str,
    schema: type[BaseModel],
    *,
    model: str,
    call_id: str,
    db: DB,
) -> BaseModel | None:
    """Run a final structured-call to coerce the freeform answer into ``schema``.

    Run AFTER the agent has emitted its freeform answer (the user's
    chosen approach, not finalize-as-structured-call). Failure surfaces
    as a None return + a logged warning rather than raising, so the
    caller still gets ``answer_text`` even when validation fails.
    """
    sys_prompt = (
        "You are a strict JSON formatter. The user message contains an answer "
        "produced by a research agent and a JSON schema. Return a single JSON "
        "object that satisfies the schema, populated from the answer text. Do "
        "not invent content; if a required field has no support in the answer, "
        "use the closest reasonable approximation and keep prose-form fields "
        "verbatim where possible."
    )
    user_message = (
        "## Answer text\n"
        f"{answer_text}\n\n"
        "## Schema\n"
        f"```json\n{json.dumps(schema.model_json_schema(), indent=2)}\n```\n"
    )
    try:
        result = await structured_call(
            sys_prompt,
            user_message,
            schema,
            metadata=LLMExchangeMetadata(
                call_id=call_id,
                phase="finalize_validation",
            ),
            db=db,
            model=model,
        )
        return result.parsed
    except (ValidationError, Exception):
        log.exception("SimpleSpine: finalize validation failed; returning None")
        return None
