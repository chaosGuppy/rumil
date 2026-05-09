"""SimpleSpineOrchestrator — structured-rounds main loop with parallel spawns.

Runs the mainline agent on a persistent thread:

- One assistant turn per "round". Each turn may include reasoning text
  + ``finalize`` + N parallel spawn tool calls.
- Spawn tool calls in the same turn are executed concurrently via
  ``asyncio.gather``; each spawn does its own internal config-prep step
  (when its ``SubroutineDef`` declares one) before running.
- Token budget is the only hard cap. Wall-clock is surfaced as a soft
  signal in the per-round system reminder; the agent self-paces. A
  loop-prevention turn ceiling (``_HARD_MAX_ROUNDS``) backstops a model
  that never finalizes and never spends its tokens.
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
from anthropic.types.beta import BetaTextBlock, BetaToolUseBlock
from pydantic import BaseModel, ValidationError

from rumil.calls.common import mark_call_completed
from rumil.database import DB
from rumil.llm import (
    LLMExchangeMetadata,
    Tool,
    call_anthropic_api,
    structured_call,
    text_call,
)
from rumil.model_config import ModelConfig
from rumil.models import Call, CallStatus, CallType
from rumil.orchestrators.simple_spine.agent_loop import strip_orphaned_server_tool_uses
from rumil.orchestrators.simple_spine.artifacts import ArtifactStore
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
from rumil.orchestrators.simple_spine.tools import (
    make_finalize_tool,
    make_read_artifact_tool,
    make_search_artifacts_tool,
)
from rumil.orchestrators.simple_spine.trace_events import (
    SpineCompactedEvent,
    SpineConfigPrepEvent,
    SpineFinalizedEvent,
    SpineRoundStartedEvent,
    SpineSpawnCompletedEvent,
    SpineSpawnStartedEvent,
    SpineThrottledEvent,
)
from rumil.settings import get_settings
from rumil.tracing import get_langfuse, observe, phase_span, propagate_attributes
from rumil.tracing.broadcast import Broadcaster
from rumil.tracing.tracer import CallTrace, reset_trace, set_trace

log = logging.getLogger(__name__)

# Loop-prevention ceiling on assistant turns. Prevents infinite loops
# if the model never finalizes and never runs out of tokens. Token cap
# is the real budget primitive — this is just a backstop.
_HARD_MAX_ROUNDS = 50


def _tool_result(tool_use_id: str, content: str) -> dict:
    return {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}


def _tool_result_error(tool_use_id: str, content: str) -> dict:
    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": content,
        "is_error": True,
    }


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

    @observe(name="orchestrator.simple_spine")
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
                "max_cost_usd": clock.spec.max_cost_usd,
                "wall_clock_soft_s": clock.spec.wall_clock_soft_s,
                "library": [s.name for s in self.config.process_library],
            },
        )
        await self.db.update_call_status(call.id, CallStatus.RUNNING)
        lf = get_langfuse()
        if lf is not None:
            lf.update_current_span(
                name=f"orchestrator.simple_spine[{self.config.fingerprint_short}]",
                metadata={
                    "call_id": call.id,
                    "call_type": call_type.value,
                    "question_id": inputs.question_id,
                    "parent_call_id": parent_call_id,
                    "fingerprint": self.config.fingerprint_short,
                    "max_cost_usd": clock.spec.max_cost_usd,
                    "library": [s.name for s in self.config.process_library],
                },
            )
        trace = CallTrace(call.id, self.db, broadcaster=self.broadcaster)
        trace_token = set_trace(trace)
        try:
            with propagate_attributes(
                session_id=self.db.run_id or None,
                metadata={
                    "orchestrator": "simple_spine",
                    "fingerprint": self.config.fingerprint_short,
                    "call_id": call.id,
                },
                tags=["orchestrator:simple_spine"],
            ):
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

        # Caller-seeded k,v store; spawn outputs are folded in
        # post-spawn under <name>/<spawn_id>[/<sub_key>] keys. Lazy
        # announcements (seeds in initial user message, spawn outputs
        # appended to tool_result content) keep token cost low.
        artifact_store = ArtifactStore(seed=dict(inputs.artifacts))
        # Fall through to preset-level defaults when caller-supplied
        # OrchInputs leaves the deliverable-shaping fields unset. Lets a
        # YAML preset (e.g. view_freeform) be self-describing — caller
        # picks the preset and gets view-shaped output without separately
        # passing schema + guidance.
        effective_output_guidance = (
            inputs.output_guidance
            if inputs.output_guidance.strip()
            else (self.config.default_output_guidance or "")
        )
        effective_output_schema: type[BaseModel] | dict[str, Any] | None
        if inputs.output_schema is not None:
            effective_output_schema = inputs.output_schema
        elif self.config.default_output_schema is not None:
            effective_output_schema = dict(self.config.default_output_schema)
        else:
            effective_output_schema = None
        initial_user = _build_initial_user_message(
            question_id=inputs.question_id,
            question_headline=question.headline,
            question_content=question.content,
            inputs=inputs,
            clock=clock,
            artifact_store=artifact_store,
            output_guidance=effective_output_guidance,
            output_schema=effective_output_schema,
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
        if self.config.expose_artifact_tools:
            all_tools.append(make_read_artifact_tool(artifact_store))
            all_tools.append(make_search_artifacts_tool(artifact_store))

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
        round_idx = 0

        compaction_kwargs: dict = {}
        if self.config.enable_server_compaction:
            edit: dict[str, Any] = {
                "type": "compact_20260112",
                "trigger": {
                    "type": "input_tokens",
                    "value": self.config.compaction_trigger_tokens,
                },
            }
            if self.config.compaction_instructions:
                edit["instructions"] = self.config.compaction_instructions
            compaction_kwargs = {
                "context_management": {"edits": [edit]},
                "betas": ["compact-2026-01-12"],
            }

        for round_idx in range(_HARD_MAX_ROUNDS):
            with phase_span(f"round_{round_idx}"):
                await trace.record(
                    SpineRoundStartedEvent(
                        round_idx=round_idx,
                        cost_usd_used=clock.cost_usd_used,
                        cost_usd_remaining=clock.cost_usd_remaining,
                        elapsed_s=clock.elapsed_s,
                    )
                )
                if clock.cost_exhausted:
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "[system] Cost budget exhausted. Call `finalize` "
                                "now with the best answer you can synthesize from "
                                "the work so far. No further spawns will run."
                            ),
                        }
                    )

                # Mainline ModelConfig — knobs live on SimpleSpineConfig so
                # presets (versus vs. research) can pin values that match
                # their finalize.answer payload size. Versus pins
                # ``mainline_max_tokens=32_000`` so the deliverable doesn't
                # get truncated when finalize lands in a single turn.
                cfg = ModelConfig(
                    temperature=self.config.mainline_temperature,
                    max_tokens=self.config.mainline_max_tokens,
                )
                with phase_span("mainline"):
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
                        **compaction_kwargs,
                    )
                response = api_resp.message
                usage = response.usage
                if usage is not None:
                    # record_exchange folds in compaction-iteration tokens
                    # internally and computes USD via pricing.compute_cost,
                    # so all four token classes (input, output, cache_create,
                    # cache_read) hit the budget at their per-model rates.
                    clock.record_exchange(usage, self.config.main_model)

                assistant_text = ""
                tool_uses: list[ToolUseBlock] = []
                for block in response.content:
                    if isinstance(block, (TextBlock, BetaTextBlock)):
                        assistant_text += block.text
                    elif isinstance(block, (ToolUseBlock, BetaToolUseBlock)):
                        tool_uses.append(block)  # pyright: ignore[reportArgumentType]
                    elif getattr(block, "type", None) == "compaction":
                        summary = getattr(block, "content", None) or ""
                        await trace.record(
                            SpineCompactedEvent(
                                round_idx=round_idx,
                                summary_chars=len(summary),
                                summary_text=summary,
                            )
                        )
                messages.append(
                    {
                        "role": "assistant",
                        "content": strip_orphaned_server_tool_uses(response.content),
                    }
                )

                if not tool_uses:
                    if clock.cost_exhausted and self.config.force_finalize_on_token_exhaustion:
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
                    if tu.name.startswith("spawn_"):
                        spawn_uses.append(tu)
                        continue
                    # Non-spawn tools (finalize, read_artifact, search_artifacts,
                    # any future first-class mainline tool) execute locally via
                    # tool_fn_map. Routing them through _run_spawn would treat
                    # them as subroutine kinds and fail.
                    fn = tool_fn_map.get(tu.name)
                    if fn is None:
                        tool_results.append(_tool_result_error(tu.id, f"Unknown tool: {tu.name}"))
                        continue
                    try:
                        result_str = await fn(tu.input)
                    except Exception as e:
                        result_str = f"Error: {e}"
                        log.exception("Tool %s raised", tu.name)
                    tool_results.append(_tool_result(tu.id, result_str))
                    if tu.name == "finalize":
                        finalize_reason = str(tu.input.get("reason", "")) or "model_finalize"

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
                            _tool_result_error(
                                tu.id,
                                f"[throttled] You requested {len(spawn_uses)} "
                                f"parallel spawns; only {cap} were run this "
                                "turn. Try the rest next turn if still wanted.",
                            )
                        )

                if kept_spawn_uses and not clock.cost_exhausted:
                    spawn_results = await asyncio.gather(
                        *[
                            self._run_spawn(
                                tu,
                                call_id=call.id,
                                question_id=inputs.question_id,
                                clock=clock,
                                mainline_system_prompt=effective_system_prompt,
                                mainline_messages=messages,
                                mainline_tool_uses=tool_uses,
                                round_idx=round_idx,
                                trace=trace,
                                operating_assumptions=inputs.operating_assumptions,
                                artifact_store=artifact_store,
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
                                _tool_result_error(
                                    tu.id,
                                    f"Spawn error: {type(res).__name__}: {res}",
                                )
                            )
                        else:
                            tool_results.append(_tool_result(tu.id, res.text_summary))
                elif kept_spawn_uses and clock.cost_exhausted:
                    for tu in kept_spawn_uses:
                        tool_results.append(
                            _tool_result_error(
                                tu.id,
                                "[budget] Token budget exhausted before this "
                                "spawn could run. Finalize next turn.",
                            )
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
                                if clock.cost_exhausted
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
                round_idx=round_idx,
                answer_chars=len(finalize_state["answer"]),
                reason=finalize_reason,
                synthesized=bool(finalize_state["synthesized"]),
            )
        )

        structured_answer: BaseModel | dict[str, Any] | None = None
        if effective_output_schema is not None and finalize_state["answer"]:
            structured_answer = await _validate_finalize(
                finalize_state["answer"],
                effective_output_schema,
                model=self.config.main_model,
                call_id=call.id,
                db=self.db,
            )

        await mark_call_completed(
            call,
            self.db,
            summary=(
                f"simple_spine: {last_status} "
                f"(cost=${clock.cost_usd_used:.2f}, spawns={spawn_count})"
            ),
        )

        return OrchResult(
            answer_text=finalize_state["answer"],
            structured_answer=structured_answer,
            fingerprint=self.config.fingerprint,
            call_id=call.id,
            spawn_count=spawn_count,
            cost_usd_used=clock.cost_usd_used,
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

    @observe(name="spawn")
    async def _run_spawn(
        self,
        tu: ToolUseBlock,
        *,
        call_id: str,
        question_id: str,
        clock: BudgetClock,
        mainline_system_prompt: str,
        mainline_messages: Sequence[Mapping[str, Any]],
        mainline_tool_uses: Sequence[ToolUseBlock],
        round_idx: int,
        trace: CallTrace,
        operating_assumptions: str = "",
        artifact_store: ArtifactStore,
    ) -> SubroutineResult:
        """Resolve the spawn tool by name → SubroutineDef and run it.

        Bypasses the Tool.fn path so we can record per-spawn trace events
        and run the optional config-prep call before delegating to
        ``SubroutineDef.run``.

        Validates ``include_artifacts`` against the run's
        :class:`ArtifactStore` before running the spawn — invalid keys
        raise (and become an ``is_error`` tool_result via the outer
        gather). After the spawn returns, folds ``result.produces`` into
        the store under ``<sub_name>/<spawn_id_short>[/<sub_key>]`` keys
        and appends per-key announcements to the spawn's ``text_summary``
        so mainline sees the new keys in its next turn.
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
        lf = get_langfuse()
        if lf is not None:
            lf.update_current_span(
                name=f"spawn.{sub.name}",
                metadata={
                    "subroutine": sub.name,
                    "spawn_id": spawn_id,
                    "round_idx": round_idx,
                },
            )
        raw_include = tu.input.get("include_artifacts") or ()
        if not isinstance(raw_include, (list, tuple)):
            raise ValueError(
                f"spawn {sub.name!r}: include_artifacts must be a list of "
                f"artifact keys (strings), got {type(raw_include).__name__}"
            )
        include_artifacts = tuple(str(k) for k in raw_include)
        missing_keys = artifact_store.require_keys(include_artifacts)
        if missing_keys:
            raise ValueError(
                f"spawn {sub.name!r}: unknown artifact key(s) in "
                f"include_artifacts: {missing_keys}. Available keys: "
                f"{artifact_store.list_keys()}"
            )
        await trace.record(
            SpineSpawnStartedEvent(
                round_idx=round_idx,
                spawn_id=spawn_id,
                subroutine_name=sub.name,
                overrides=dict(tu.input),
            )
        )
        # Carve a per-spawn BudgetClock so accounting is accurate under
        # parallel spawns: under asyncio.gather, sibling spawns share one
        # clock and a parent-delta would double-count whichever spawn
        # finishes last. Each kind's `carve_spawn_clock` decides how —
        # default carves a child from the parent (record_tokens still
        # bubbles up so the run-level cap is enforced); CallType returns
        # the parent because its LLM calls bypass the SimpleSpine clock.
        raw_cap = tu.input.get("cost_cap_usd") if "cost_cap_usd" in sub.overridable else None
        override_cap = float(raw_cap) if isinstance(raw_cap, (int, float, str)) else None
        spawn_clock = sub.carve_spawn_clock(clock, override_cap=override_cap)
        ctx = SpawnCtx(
            db=self.db,
            budget_clock=spawn_clock,
            broadcaster=self.broadcaster,
            parent_call_id=call_id,
            question_id=question_id,
            spawn_id=spawn_id,
            operating_assumptions=operating_assumptions,
            artifacts=artifact_store,
            include_artifacts=include_artifacts,
        )
        if sub.config_prep is not None:
            ctx.prepped_config = await self._run_config_prep(
                sub,
                ctx,
                tu,
                call_id=call_id,
                mainline_system_prompt=mainline_system_prompt,
                mainline_messages=mainline_messages,
                mainline_tool_uses=mainline_tool_uses,
                round_idx=round_idx,
                trace=trace,
            )
        cost_before = spawn_clock.cost_usd_used
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
        cost_consumed = max(spawn_clock.cost_usd_used - cost_before, 0.0)
        # Fold produces into the store. Empty sub-key → <name>/<spawn_id_short>;
        # non-empty sub-key → <name>/<spawn_id_short>/<sub_key>. Skip
        # empty-text entries (no point announcing a key with no content).
        new_keys: list[str] = []
        for sub_key, text in result.produces.items():
            if not text:
                continue
            full_key = _make_artifact_key(sub.name, spawn_id, sub_key)
            artifact_store.add(
                full_key,
                text,
                produced_by=sub.name,
                spawn_id=spawn_id,
                round_idx=round_idx,
            )
            new_keys.append(full_key)
        announcement_block = (
            "\n\n" + "\n".join(artifact_store.announce(k) for k in new_keys) if new_keys else ""
        )
        result = SubroutineResult(
            text_summary=(
                f"[spawn cost: ${cost_consumed:.3f}]\n{result.text_summary}{announcement_block}"
            ),
            cost_usd_used=result.cost_usd_used,
            extra={
                **dict(result.extra),
                "cost_usd_consumed": cost_consumed,
                "produced_artifact_keys": new_keys,
            },
            produces=dict(result.produces),
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

    @observe(name="config_prep")
    async def _run_config_prep(
        self,
        sub: SubroutineDef,
        ctx: SpawnCtx,
        target_tu: ToolUseBlock,
        *,
        call_id: str,
        mainline_system_prompt: str,
        mainline_messages: Sequence[Mapping[str, Any]],
        mainline_tool_uses: Sequence[ToolUseBlock],
        round_idx: int,
        trace: CallTrace,
    ) -> BaseModel | None:
        """Branch off mainline to elaborate ``target_tu`` into a full config.

        Inherits mainline's system prompt and full message history (which
        already ends with the assistant turn that issued ``target_tu``).
        Synthesizes ``tool_result`` blocks for *every* tool_use in that
        trailing assistant turn — the Anthropic API rejects a follow-up
        user turn that omits any tool_use_id — then appends a text
        instruction asking the elaborator to produce the structured
        config. Tools are deliberately omitted from the request: we want
        structured output, not another tool call, and dropping tools
        keeps the request well-formed for ``messages.parse``.

        Sibling tool_uses (other parallel spawns, ``finalize``, etc.) get
        a "deferred — branched" placeholder. The branch never affects
        mainline's actual execution; the real spawn runs after this prep
        call returns.
        """
        prep = sub.config_prep
        assert prep is not None
        lf = get_langfuse()
        if lf is not None:
            lf.update_current_span(
                name=f"config_prep.{sub.name}",
                metadata={
                    "subroutine": sub.name,
                    "spawn_id": ctx.spawn_id,
                    "round_idx": round_idx,
                },
            )
        synthetic_user = _build_prep_user_turn(
            sub_name=sub.name,
            target_tu=target_tu,
            sibling_tool_uses=mainline_tool_uses,
            instructions=prep.instructions,
        )
        prep_messages = [*mainline_messages, {"role": "user", "content": synthetic_user}]
        prepped_result = await structured_call(
            mainline_system_prompt,
            messages=prep_messages,
            response_model=prep.output_schema,
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


def _make_artifact_key(sub_name: str, spawn_id: str, sub_key: str) -> str:
    """Build the namespaced ArtifactStore key for a spawn-produced entry.

    Empty ``sub_key`` (the default produces shape ``{"": text}``) becomes
    ``<sub_name>/<spawn_id_short>``; non-empty sub-keys become
    ``<sub_name>/<spawn_id_short>/<sub_key>``. The 8-char spawn-id
    suffix is enough to disambiguate concurrent spawns within a run; the
    full uuid lives on the trace event for forensics.
    """
    short = spawn_id[:8]
    if sub_key:
        return f"{sub_name}/{short}/{sub_key}"
    return f"{sub_name}/{short}"


def _format_tool_description(sub: SubroutineDef) -> str:
    """Compose the spawn tool description shown to mainline.

    Layered on top of the author-supplied ``sub.description``:
    - a ``2-step`` annotation when ``config_prep`` is set, so mainline
      knows there's a hidden elaboration call that branches off the
      current conversation (same system + history) to fill in the
      subroutine's full config from this thin spawn payload;
    - an author-supplied ``cost_hint`` line so mainline can plan its
      first spawn before live ``[spawn cost: …]`` feedback arrives.
    """
    parts: list[str] = [sub.description.rstrip()]
    if sub.config_prep is not None:
        parts.append(
            "_(2-step: thin intent → elaborator branches off this "
            "conversation, sees the same system + history, fills in "
            "the full sys/user/tools config)_"
        )
    cost_hint = getattr(sub, "cost_hint", None)
    if cost_hint:
        parts.append(f"Cost hint: {cost_hint}")
    return "\n\n".join(parts)


def _build_prep_user_turn(
    *,
    sub_name: str,
    target_tu: ToolUseBlock,
    sibling_tool_uses: Sequence[ToolUseBlock],
    instructions: str,
) -> list[dict]:
    """Synthesize the user turn that follows mainline's trailing assistant turn.

    Anthropic requires a ``tool_result`` for every ``tool_use_id`` in
    the prior assistant turn — without that the API rejects with 400.
    Since the spawn hasn't actually run, every result here is a
    placeholder. The target spawn's placeholder explicitly frames the
    branch ("now elaborate this config"); siblings get a generic
    "deferred for branched elaboration" so they don't influence the
    elaboration. A trailing text block carries the elaboration request
    and any kind-specific ``instructions``.
    """
    blocks: list[dict] = []
    for tu in sibling_tool_uses:
        if tu.id == target_tu.id:
            blocks.append(
                _tool_result(
                    tu.id,
                    f"[branched] Before this `{tu.name}` runs, the "
                    "harness is elaborating its full config — see "
                    "the request below.",
                )
            )
        else:
            blocks.append(
                _tool_result(
                    tu.id,
                    "[branched] Skipped for this elaboration branch; "
                    "this tool_use will be handled in mainline's real flow.",
                )
            )
    intent_payload_text = json.dumps(dict(target_tu.input), ensure_ascii=False, indent=2)
    elaboration = (
        f"You just called `spawn_{sub_name}` with this thin payload:\n\n"
        f"```json\n{intent_payload_text}\n```\n\n"
        "Before that spawn runs, produce the **full structured config** "
        "for it as your next response. Reply with structured output "
        "matching the elaborator schema — do not call another tool. "
        "Tune sys_prompts, user_prompts, tool selections, and any "
        "kind-specific fields to mainline's current state and the "
        "intent above."
    )
    if instructions.strip():
        elaboration = elaboration + "\n\n" + instructions.strip()
    blocks.append({"type": "text", "text": elaboration})
    return blocks


def _build_initial_user_message(
    *,
    question_id: str,
    question_headline: str,
    question_content: str,
    inputs: OrchInputs,
    clock: BudgetClock,
    artifact_store: ArtifactStore,
    output_guidance: str,
    output_schema: type[BaseModel] | dict[str, Any] | None,
) -> str:
    # When the caller seeds artifacts, treat the artifact channel as
    # the canonical surface for content and skip question.content in
    # the initial mainline prompt — otherwise pair / rubric / etc.
    # would render twice (once raw via question.content, once
    # XML-fenced via render_seed_block). The question_id + headline
    # still render as a workspace anchor. Callers without artifacts
    # (research, essay_continuation today) get the existing behavior:
    # question.content is the primary surface and renders verbatim.
    has_artifacts = bool(inputs.artifacts)
    sections: list[str] = [
        "## Scope question",
        f"`{question_id}` — {question_headline}",
        "",
    ]
    if not has_artifacts:
        sections.append(question_content.strip())
        sections.append("")
    if inputs.additional_context.strip():
        sections.append("## Additional context (from caller)")
        sections.append(inputs.additional_context.strip())
        sections.append("")
    if output_guidance.strip():
        sections.append("## Output guidance")
        sections.append(output_guidance.strip())
        sections.append("")
    if output_schema is not None:
        sections.append("## Output schema (your finalize answer will be parsed against this)")
        schema_json = (
            output_schema if isinstance(output_schema, dict) else output_schema.model_json_schema()
        )
        sections.append(f"```json\n{json.dumps(schema_json, indent=2)}\n```")
        sections.append("")
    seed_block = artifact_store.render_seed_block()
    if seed_block:
        # Render input-seeded artifacts with content (XML-fenced) so
        # mainline sees keys + bodies together on its first turn — same
        # demarcation subroutines see when consumes / include_artifacts
        # splices them in. Spawn-produced artifacts get announced
        # lazily one-line in tool_result messages as they're created;
        # no persistent registry block per round.
        sections.append(seed_block.rstrip())
        sections.append("")
        sections.append(
            "Reference these artifact keys (and any new keys announced "
            "in tool_result messages) by passing `include_artifacts: "
            "[<key>, ...]` on future spawn calls. Subroutines also have "
            "a static `consumes` declaration that's always spliced "
            "regardless."
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
    schema: type[BaseModel] | dict[str, Any],
    *,
    model: str,
    call_id: str,
    db: DB,
) -> BaseModel | dict[str, Any] | None:
    """Run a final coercion call to shape the freeform answer into ``schema``.

    Run AFTER the agent has emitted its freeform answer. Pydantic schemas
    go through ``structured_call`` for full schema enforcement; raw JSON
    Schema dicts go through a ``text_call`` + ``json.loads`` (no
    schema-side validation — callers parse / validate themselves on the
    dict path). Failure surfaces as a None return + a logged warning
    rather than raising, so the caller still gets ``answer_text`` even
    when coercion fails.
    """
    sys_prompt = (
        "You are a strict JSON formatter. The user message contains an answer "
        "produced by a research agent and a JSON schema. Return a single JSON "
        "object that satisfies the schema, populated from the answer text. Do "
        "not invent content; if a required field has no support in the answer, "
        "use the closest reasonable approximation and keep prose-form fields "
        "verbatim where possible."
    )
    schema_json = schema if isinstance(schema, dict) else schema.model_json_schema()
    user_message = (
        "## Answer text\n"
        f"{answer_text}\n\n"
        "## Schema\n"
        f"```json\n{json.dumps(schema_json, indent=2)}\n```\n"
    )
    if isinstance(schema, dict):
        try:
            text = await text_call(
                sys_prompt + " Respond with only the JSON object, no prose.",
                user_message,
                metadata=LLMExchangeMetadata(
                    call_id=call_id,
                    phase="finalize_validation",
                ),
                db=db,
                model=model,
            )
            parsed = json.loads(_strip_json_fence(text))
            if not isinstance(parsed, dict):
                log.warning(
                    "SimpleSpine: finalize coercion returned non-object JSON; returning None"
                )
                return None
            return parsed
        except (json.JSONDecodeError, Exception):
            log.exception("SimpleSpine: finalize coercion (dict schema) failed; returning None")
            return None
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


def _strip_json_fence(text: str) -> str:
    """Strip a ```json ... ``` fence from a model response, if present."""
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s[3:]
        if s.endswith("```"):
            s = s[: -len("```")].rstrip()
    return s
