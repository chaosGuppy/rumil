"""AxonOrchestrator — mainline loop + two-step delegate dispatch.

Architecture (matches design discussion):

- Mainline runs as one persistent thread. Tools are fixed at run start
  (delegate, configure, finalize, plus configured direct tools) so the
  cache prefix never invalidates.
- Each assistant turn may emit multiple ``delegate`` calls in parallel,
  plus direct tool calls and an optional ``finalize``.
- For each ``delegate`` call, the orchestrator runs a *configure
  follow-up*: an API call that's a continuation of the spine thread
  (placeholder tool_results + directive identifying which delegate to
  configure). The model returns a :class:`DelegateConfig` via the
  configure tool. This call hits cache on the entire spine prefix.
- Configs are validated for the continuation/isolation coupling rule.
  Violations trigger a corrective re-fire (bounded retry).
- For each (delegate, config) pair, the orchestrator runs an inner loop
  via :func:`run_inner_loop`. ``inherit_context=True`` seeds the inner
  loop with the spine's messages + a framing user message; ``False``
  starts fresh. The inner loop terminates by calling ``finalize``; the
  payload is validated against ``cfg.finalize_schema``.
- After all parallel delegates complete (gather), real tool_results
  replace the placeholders and the spine takes its next turn.

This first version implements the core flow for ``n=1`` delegates with
``artifact_key`` side effects. ``n>1`` is a follow-up.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anthropic
from anthropic.types import TextBlock, ToolUseBlock
from anthropic.types.beta import BetaTextBlock, BetaToolUseBlock
from pydantic import ValidationError

from rumil.calls.common import mark_call_completed, prepare_tools
from rumil.database import DB
from rumil.llm import LLMExchangeMetadata, Tool, call_anthropic_api
from rumil.models import Call, CallType
from rumil.orchestrators.axon.artifacts import ArtifactStore
from rumil.orchestrators.axon.budget_clock import BudgetClock, BudgetSpec
from rumil.orchestrators.axon.config import (
    OPERATING_ASSUMPTIONS_KEY,
    AxonConfig,
    OrchInputs,
    OrchResult,
    build_initial_artifacts,
)
from rumil.orchestrators.axon.runner import (
    InnerLoopResult,
    run_inner_loop,
    validate_finalize_payload,
)
from rumil.orchestrators.axon.schemas import (
    DelegateConfig,
    DelegateRequest,
    FinalizeSchemaSpec,
    SystemPromptSpec,
)
from rumil.orchestrators.axon.tools import (
    CONFIGURE_TOOL_NAME,
    DELEGATE_TOOL_NAME,
    FINALIZE_TOOL_NAME,
    build_finalize_tool,
    build_mainline_tools,
    resolve_direct_tools,
)
from rumil.orchestrators.axon.trace_events import (
    AxonConfigurePreparedEvent,
    AxonConfigureRetriedEvent,
    AxonDelegateCompletedEvent,
    AxonDelegateRequestedEvent,
    AxonFinalizedEvent,
    AxonInnerLoopCompletedEvent,
    AxonInnerLoopStartedEvent,
    AxonRoundStartedEvent,
    AxonRunStartedEvent,
    AxonSideEffectAppliedEvent,
)
from rumil.settings import get_settings
from rumil.tracing.broadcast import Broadcaster
from rumil.tracing.tracer import CallTrace, reset_trace, set_trace

log = logging.getLogger(__name__)

_HARD_MAX_ROUNDS_FALLBACK = 50
_MAX_CONFIGURE_RETRIES = 2
_CONFIGURE_PLACEHOLDER = "[awaiting configure]"


@dataclass
class _PendingDelegate:
    """One delegate call collected from a mainline assistant turn."""

    tool_use_id: str
    request: DelegateRequest
    request_index: int  # index within this turn's parallel delegates


async def _gather_delegate_outcomes(
    coros: Iterable[Awaitable[_DelegateOutcome]],
) -> list[_DelegateOutcome]:
    """Typed wrapper around ``asyncio.gather`` for delegate outcomes.

    Pyright's overload inference for ``asyncio.gather`` confuses
    ``_DelegateOutcome`` with sibling-typed return values when the call
    is inline (likely picks the variadic-positionals-with-mixed-Ts
    overload). Wrapping in this helper with an explicit return
    annotation pins the type and keeps the dispatch loop pyright-clean.
    """
    coro_list = list(coros)
    if not coro_list:
        return []
    return list(await asyncio.gather(*coro_list, return_exceptions=False))


async def _gather_sample_results(
    coros: Iterable[Awaitable[tuple[int, dict[str, Any] | None, str | None, str]]],
) -> list[tuple[int, dict[str, Any] | None, str | None, str]]:
    """Typed wrapper around ``asyncio.gather`` for n-sample results.

    Same pyright-pinning trick as :func:`_gather_delegate_outcomes`.
    Returns the per-sample tuples in input (sample_idx) order — gather's
    contract regardless of completion order — so downstream side-effect
    application stays deterministic.
    """
    coro_list = list(coros)
    if not coro_list:
        return []
    return list(await asyncio.gather(*coro_list, return_exceptions=False))


@dataclass
class _DelegateOutcome:
    """Result of running one delegate end-to-end (configure + inner loop + side effects)."""

    tool_use_id: str
    tool_result_content: str
    is_error: bool
    cost_usd_used: float


class AxonOrchestrator:
    """Run an axon mainline loop with two-step delegate dispatch."""

    def __init__(
        self,
        db: DB,
        config: AxonConfig,
        broadcaster: Broadcaster | None = None,
    ) -> None:
        self.db = db
        self.config = config
        self.broadcaster = broadcaster
        self._client: anthropic.AsyncAnthropic | None = None

    @property
    def client(self) -> anthropic.AsyncAnthropic:
        if self._client is None:
            self._client = anthropic.AsyncAnthropic(api_key=get_settings().require_anthropic_key())
        return self._client

    async def run(
        self,
        inputs: OrchInputs,
        *,
        call_type: CallType = CallType.CLAUDE_CODE_DIRECT,
        parent_call_id: str | None = None,
    ) -> OrchResult:
        """Top-level entry point. Returns the final answer + run metadata."""
        # Materialise the runs row first — without it the trace UI 404s
        # on the run_id even though the calls exist. The DB carries the
        # run_id from DB.create(); this is its first persisted appearance.
        # Skip if a row for this run_id already exists (nested orch
        # invocations re-using the same DB / run_id).
        if parent_call_id is None:
            try:
                existing = await self.db.get_run(self.db.run_id)
            except Exception:
                existing = None
            if existing is None:
                await self.db.create_run(
                    name=f"axon:{self.config.name}",
                    question_id=None,
                    config={
                        "orchestrator": "axon",
                        "config_name": self.config.name,
                        "main_model": self.config.main_model,
                        "budget_usd": inputs.budget_usd,
                    },
                    entrypoint="axon",
                )

        call_id = str(uuid.uuid4())
        call = await self.db.create_call(
            call_type=call_type,
            parent_call_id=parent_call_id,
            call_id=call_id,
        )
        run_id = self.db.run_id or str(uuid.uuid4())
        trace = CallTrace(call_id=call_id, db=self.db, broadcaster=self.broadcaster)
        token = set_trace(trace)
        try:
            return await self._run_inner(inputs, call, call_id, run_id, trace)
        finally:
            reset_trace(token)

    async def _run_inner(
        self,
        inputs: OrchInputs,
        call: Call,
        call_id: str,
        run_id: str,
        trace: CallTrace,
    ) -> OrchResult:
        budget_spec = BudgetSpec(
            max_cost_usd=inputs.budget_usd,
            wall_clock_soft_s=inputs.wall_clock_soft_s,
        )
        budget_clock = BudgetClock(spec=budget_spec)
        artifacts = ArtifactStore(seed=build_initial_artifacts(inputs))

        system_prompt = self._load_main_system_prompt()
        mainline_tools = build_mainline_tools(self.config.direct_tools)
        mainline_tool_defs, mainline_tool_fns = prepare_tools(mainline_tools)

        await trace.record(
            AxonRunStartedEvent(
                config_name=self.config.name,
                main_model=self.config.main_model,
                budget_usd=inputs.budget_usd,
                initial_artifact_keys=artifacts.list_keys(),
            )
        )

        # Spine message stack — accumulates over the run.
        spine_messages: list[dict] = []
        first_user_text = await self._build_initial_user_message(inputs, artifacts)
        spine_messages.append({"role": "user", "content": first_user_text})

        last_status = "incomplete"
        answer_text = ""
        rounds_used = 0
        hard_max = self.config.hard_max_rounds or _HARD_MAX_ROUNDS_FALLBACK

        for round_idx in range(hard_max):
            rounds_used = round_idx + 1
            await trace.record(
                AxonRoundStartedEvent(
                    round_idx=round_idx,
                    cost_usd_used=budget_clock.cost_usd_used,
                    cost_usd_remaining=budget_clock.cost_usd_remaining,
                )
            )

            response = await self._call_mainline(
                system_prompt=system_prompt,
                messages=spine_messages,
                tool_defs=mainline_tool_defs,
                call_id=call_id,
                phase="mainline",
                round_idx=round_idx,
                budget_clock=budget_clock,
            )

            text_parts: list[str] = []
            tool_uses: list[ToolUseBlock] = []
            for block in response.content:
                if isinstance(block, (TextBlock, BetaTextBlock)):
                    text_parts.append(block.text)
                elif isinstance(block, (ToolUseBlock, BetaToolUseBlock)):
                    tool_uses.append(block)  # pyright: ignore[reportArgumentType]
            assistant_text = "\n".join(text_parts)
            spine_messages.append({"role": "assistant", "content": list(response.content)})

            if not tool_uses:
                last_status = "no_tool_calls"
                if not answer_text and assistant_text:
                    answer_text = assistant_text
                break

            finalize_block = next((tu for tu in tool_uses if tu.name == FINALIZE_TOOL_NAME), None)
            if finalize_block is not None:
                payload = dict(finalize_block.input or {})
                answer_text = str(payload.get("answer", "")).strip()
                last_status = "completed"
                # If finalize was emitted alongside other tool calls, satisfy
                # them with placeholder errors so the conversation is well-
                # formed in the trace; the loop exits regardless.
                if len(tool_uses) > 1:
                    placeholder_results: list[dict] = []
                    for tu in tool_uses:
                        if tu.id == finalize_block.id:
                            continue
                        placeholder_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": tu.id,
                                "content": "[finalize fired in same turn — peer call skipped]",
                                "is_error": True,
                            }
                        )
                    spine_messages.append({"role": "user", "content": placeholder_results})
                break

            tool_results = await self._dispatch_turn_tool_uses(
                tool_uses=tool_uses,
                spine_messages=spine_messages,
                system_prompt=system_prompt,
                mainline_tool_defs=mainline_tool_defs,
                mainline_tool_fns=mainline_tool_fns,
                artifacts=artifacts,
                budget_clock=budget_clock,
                call_id=call_id,
                trace=trace,
                round_idx=round_idx,
            )

            budget_block = self._budget_user_block(budget_clock)
            spine_messages.append({"role": "user", "content": [*tool_results, budget_block]})

            if budget_clock.cost_exhausted:
                last_status = "budget_exhausted"
                # Continue one more round so the model gets a chance to
                # finalize given the explicit signal in budget_block.
                # If it still doesn't, we exit on the next iteration.
                continue
        else:
            last_status = "max_rounds"

        await trace.record(
            AxonFinalizedEvent(
                answer_text=answer_text,
                last_status=last_status,
                rounds_used=rounds_used,
                cost_usd_used=budget_clock.cost_usd_used,
            )
        )
        await mark_call_completed(call, self.db, summary=answer_text or last_status)

        return OrchResult(
            answer_text=answer_text,
            cost_usd_used=budget_clock.cost_usd_used,
            rounds_used=rounds_used,
            last_status=last_status,
            run_id=run_id,
            call_id=call_id,
        )

    async def _dispatch_turn_tool_uses(
        self,
        *,
        tool_uses: Sequence[ToolUseBlock],
        spine_messages: list[dict],
        system_prompt: str,
        mainline_tool_defs: list[dict],
        mainline_tool_fns: dict[str, Any],
        artifacts: ArtifactStore,
        budget_clock: BudgetClock,
        call_id: str,
        trace: CallTrace,
        round_idx: int,
    ) -> list[dict]:
        """Process one turn's tool calls; return real tool_results in tool_use order.

        - delegate calls: run two-step dispatch in parallel, then return
          finalize-derived tool_results.
        - direct tool calls: dispatch immediately via the mainline tool fn.
        - configure / finalize: not expected at this layer (configure is a
          follow-up only; finalize is handled by the caller).
        """
        # Group tool_uses into delegate / direct / unexpected.
        delegate_calls: list[_PendingDelegate] = []
        direct_calls: list[ToolUseBlock] = []
        unexpected: list[ToolUseBlock] = []
        for i, tu in enumerate(tool_uses):
            if tu.name == DELEGATE_TOOL_NAME:
                try:
                    req = DelegateRequest.model_validate(dict(tu.input or {}))
                except ValidationError as e:
                    direct_calls.append(tu)  # surface as is_error tool_result
                    log.warning("delegate validation failed at idx %d: %s", i, e)
                    continue
                delegate_calls.append(
                    _PendingDelegate(
                        tool_use_id=tu.id,
                        request=req,
                        request_index=len(delegate_calls),
                    )
                )
            elif tu.name in (CONFIGURE_TOOL_NAME, FINALIZE_TOOL_NAME):
                unexpected.append(tu)
            else:
                direct_calls.append(tu)

        # Run delegate two-step dispatch (configure + inner loop) for all
        # delegates in this turn concurrently. Each ``_dispatch_one_delegate``
        # is a fully-self-contained coroutine over its own carved budget
        # clock; mutations to shared state (artifacts, trace) are
        # serialised by Python's GIL since neither does heavy CPU work.
        delegate_outcomes: dict[str, _DelegateOutcome] = {}
        if delegate_calls:
            outcomes = await _gather_delegate_outcomes(
                self._dispatch_one_delegate(
                    pending=pd,
                    all_pending=delegate_calls,
                    spine_messages=spine_messages,
                    system_prompt=system_prompt,
                    mainline_tool_defs=mainline_tool_defs,
                    artifacts=artifacts,
                    parent_budget_clock=budget_clock,
                    call_id=call_id,
                    trace=trace,
                    round_idx=round_idx,
                )
                for pd in delegate_calls
            )
            for outcome in outcomes:
                delegate_outcomes[outcome.tool_use_id] = outcome

        # Run direct tools (sequentially; usually only a handful per turn).
        direct_results: dict[str, tuple[str, bool]] = {}
        for tu in direct_calls:
            fn = mainline_tool_fns.get(tu.name)
            if fn is None:
                direct_results[tu.id] = (f"Unknown tool: {tu.name}", True)
                continue
            try:
                result = await fn(tu.input)
                direct_results[tu.id] = (result, False)
            except Exception as e:
                log.exception("direct tool %s raised", tu.name)
                direct_results[tu.id] = (f"Error: {e}", True)

        # Surface unexpected tool calls (configure / finalize at non-followup).
        for tu in unexpected:
            direct_results[tu.id] = (
                f"{tu.name} is not callable directly from the mainline turn — "
                "configure is only valid in configure follow-up turns; "
                "finalize is the run-terminator (it exits the loop on its own).",
                True,
            )

        # Assemble tool_results in the original tool_use order.
        out: list[dict] = []
        for tu in tool_uses:
            if tu.id in delegate_outcomes:
                outcome = delegate_outcomes[tu.id]
                out.append(
                    self._tool_result_block(tu.id, outcome.tool_result_content, outcome.is_error)
                )
            elif tu.id in direct_results:
                content, is_err = direct_results[tu.id]
                out.append(self._tool_result_block(tu.id, content, is_err))
            else:
                out.append(self._tool_result_block(tu.id, "[no result]", True))
        return out

    async def _dispatch_one_delegate(
        self,
        *,
        pending: _PendingDelegate,
        all_pending: Sequence[_PendingDelegate],
        spine_messages: list[dict],
        system_prompt: str,
        mainline_tool_defs: list[dict],
        artifacts: ArtifactStore,
        parent_budget_clock: BudgetClock,
        call_id: str,
        trace: CallTrace,
        round_idx: int,
    ) -> _DelegateOutcome:
        """Run configure → inner loop → side effects for one delegate."""
        delegate_id = uuid.uuid4().hex[:8]
        req = pending.request
        await trace.record(
            AxonDelegateRequestedEvent(
                round_idx=round_idx,
                delegate_id=delegate_id,
                tool_use_id=pending.tool_use_id,
                intent=req.intent,
                inherit_context=req.inherit_context,
                budget_usd=req.budget_usd,
                n=req.n,
            )
        )

        # Carve a per-delegate budget clock from the parent.
        delegate_clock = parent_budget_clock.carve_child(req.budget_usd)

        # Step 1: configure (with bounded retry on coupling-rule violations).
        try:
            cfg = await self._configure_delegate(
                pending=pending,
                all_pending=all_pending,
                spine_messages=spine_messages,
                system_prompt=system_prompt,
                mainline_tool_defs=mainline_tool_defs,
                budget_clock=delegate_clock,
                call_id=call_id,
                trace=trace,
                delegate_id=delegate_id,
                round_idx=round_idx,
            )
        except _DelegateError as e:
            return _DelegateOutcome(
                tool_use_id=pending.tool_use_id,
                tool_result_content=str(e),
                is_error=True,
                cost_usd_used=delegate_clock.cost_usd_used,
            )

        # Step 2: inner loop(s). One configure governs all N samples;
        # samples run concurrently via asyncio.gather. asyncio.gather
        # preserves input order so sample_results is always in idx
        # order regardless of completion order — side effects + the
        # tool_result formatter below get a deterministic sequence.
        # Trace events from the parallel inner loops will interleave.
        finalize_schema = self._resolve_finalize_schema(cfg.finalize_schema)

        async def _run_one_sample(
            sample_idx: int,
        ) -> tuple[int, dict[str, Any] | None, str | None, str]:
            try:
                inner_result = await self._run_inner_for_delegate(
                    req=req,
                    cfg=cfg,
                    spine_messages=spine_messages,
                    system_prompt=system_prompt,
                    budget_clock=delegate_clock,
                    call_id=call_id,
                    delegate_id=delegate_id,
                    sample_idx=sample_idx,
                    trace=trace,
                )
            except _DelegateError as e:
                return (sample_idx, None, str(e), "")
            validated, err = validate_finalize_payload(
                inner_result.finalize_payload, finalize_schema
            )
            return (sample_idx, validated, err, inner_result.final_text)

        sample_results = await _gather_sample_results(_run_one_sample(i) for i in range(req.n))

        # Step 3: side effects + tool_result formatting.
        artifact_keys_written: list[str] = []
        for sample_idx, validated, _err, _final_text in sample_results:
            if validated is None:
                continue
            if "write_artifact" in cfg.side_effects and cfg.artifact_key:
                key = cfg.artifact_key if req.n == 1 else f"{cfg.artifact_key}/{sample_idx}"
                try:
                    payload_text = json.dumps(validated, ensure_ascii=False, indent=2)
                    spawn_id = delegate_id if req.n == 1 else f"{delegate_id}/{sample_idx}"
                    artifacts.add(
                        key,
                        payload_text,
                        produced_by=delegate_id,
                        spawn_id=spawn_id,
                        round_idx=round_idx,
                    )
                    artifact_keys_written.append(key)
                    await trace.record(
                        AxonSideEffectAppliedEvent(
                            delegate_id=delegate_id,
                            sample_idx=sample_idx,
                            kind="write_artifact",
                            detail={"key": key, "chars": len(payload_text)},
                        )
                    )
                except ValueError as e:
                    log.warning("axon: artifact write failed: %s", e)

        await trace.record(
            AxonDelegateCompletedEvent(
                delegate_id=delegate_id,
                n=req.n,
                cost_usd_used=delegate_clock.cost_usd_used,
            )
        )

        # Compose tool_result content. n==1: format the single payload.
        # n>1: list the per-sample payloads (or per-sample errors) with
        # artifact keys when applicable.
        ok_samples = [(i, p) for i, p, _e, _t in sample_results if p is not None]
        if req.n == 1:
            if not ok_samples:
                _i, _p, err, last_text = sample_results[0]
                return _DelegateOutcome(
                    tool_use_id=pending.tool_use_id,
                    tool_result_content=(
                        f"[delegate {delegate_id} did not finalize cleanly: {err}; "
                        f"last text: {last_text[:1000] or '<empty>'}]"
                    ),
                    is_error=True,
                    cost_usd_used=delegate_clock.cost_usd_used,
                )
            return _DelegateOutcome(
                tool_use_id=pending.tool_use_id,
                tool_result_content=self._format_delegate_result(
                    ok_samples[0][1],
                    artifact_keys_written[0] if artifact_keys_written else None,
                ),
                is_error=False,
                cost_usd_used=delegate_clock.cost_usd_used,
            )

        # n > 1
        all_failed = not ok_samples
        return _DelegateOutcome(
            tool_use_id=pending.tool_use_id,
            tool_result_content=self._format_n_sample_result(
                sample_results,
                artifact_keys_written,
            ),
            is_error=all_failed,
            cost_usd_used=delegate_clock.cost_usd_used,
        )

    def _format_n_sample_result(
        self,
        sample_results: Sequence[tuple[int, dict[str, Any] | None, str | None, str]],
        artifact_keys_written: Sequence[str],
    ) -> str:
        """Render n>1 results as a list back to mainline."""
        lines: list[str] = [f"[n={len(sample_results)} samples]"]
        if artifact_keys_written:
            lines.append(f"persisted to artifacts: {list(artifact_keys_written)}")
        lines.append("")
        for idx, payload, err, last_text in sample_results:
            if payload is not None:
                lines.append(f"### sample {idx}")
                lines.append(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                lines.append(f"### sample {idx} (failed)")
                lines.append(f"error: {err}")
                if last_text:
                    lines.append(f"last text: {last_text[:500]}")
            lines.append("")
        return "\n".join(lines)

    async def _configure_delegate(
        self,
        *,
        pending: _PendingDelegate,
        all_pending: Sequence[_PendingDelegate],
        spine_messages: list[dict],
        system_prompt: str,
        mainline_tool_defs: list[dict],
        budget_clock: BudgetClock,
        call_id: str,
        trace: CallTrace,
        delegate_id: str,
        round_idx: int,
    ) -> DelegateConfig:
        """Run the configure follow-up call(s) until a valid DelegateConfig lands.

        Bounded retry on coupling-rule violations: append a corrective
        user message and re-fire. After ``_MAX_CONFIGURE_RETRIES`` we
        give up and surface an error.
        """
        corrective: str | None = None
        for attempt in range(_MAX_CONFIGURE_RETRIES + 1):
            fork_messages = self._build_configure_fork_messages(
                spine_messages=spine_messages,
                all_pending=all_pending,
                target=pending,
                corrective=corrective,
            )
            response = await self._call_mainline(
                system_prompt=system_prompt,
                messages=fork_messages,
                tool_defs=mainline_tool_defs,
                call_id=call_id,
                phase=f"configure[{delegate_id}]",
                round_idx=round_idx,
                budget_clock=budget_clock,
            )
            cfg, err = self._extract_configure_call(response, pending.request)
            if cfg is not None:
                await trace.record(
                    AxonConfigurePreparedEvent(
                        delegate_id=delegate_id,
                        config=cfg.model_dump(),
                        rationale=cfg.rationale,
                        cost_usd_used=budget_clock.cost_usd_used,
                    )
                )
                return cfg
            await trace.record(
                AxonConfigureRetriedEvent(
                    delegate_id=delegate_id,
                    attempt=attempt + 1,
                    reason=err or "unknown",
                )
            )
            corrective = err
        raise _DelegateError(
            f"configure follow-up failed after {_MAX_CONFIGURE_RETRIES + 1} attempts: {corrective}"
        )

    def _build_configure_fork_messages(
        self,
        *,
        spine_messages: Sequence[dict],
        all_pending: Sequence[_PendingDelegate],
        target: _PendingDelegate,
        corrective: str | None,
    ) -> list[dict]:
        """Build the message stack for one configure follow-up call.

        Spine prefix + a single user-role block containing placeholder
        tool_results (one per parallel delegate, identical text for
        cache uniformity) + a directive identifying the target +
        optional corrective on retry.
        """
        placeholders: list[dict] = []
        for pd in all_pending:
            placeholders.append(
                {
                    "type": "tool_result",
                    "tool_use_id": pd.tool_use_id,
                    "content": _CONFIGURE_PLACEHOLDER,
                }
            )
        directive_lines = [
            f"Configure delegate at index {target.request_index} now by calling `configure`.",
            "Inputs: "
            + json.dumps(
                {
                    "intent": target.request.intent,
                    "inherit_context": target.request.inherit_context,
                    "budget_usd": target.request.budget_usd,
                    "n": target.request.n,
                }
            ),
            "If inherit_context=True: leave system_prompt and tools as null (the inner loop reuses my system + tools — that's the cache-shared continuation).",
            "If inherit_context=False: pick a system_prompt (ref or inline) and a tools subset.",
            "Set finalize_schema to whatever shape the result should come back as.",
        ]
        if corrective:
            directive_lines.append(f"\n[retry] Previous configure was rejected: {corrective}")
        directive = {"type": "text", "text": "\n".join(directive_lines)}
        return [
            *spine_messages,
            {"role": "user", "content": [*placeholders, directive]},
        ]

    def _extract_configure_call(
        self,
        response: anthropic.types.Message,
        request: DelegateRequest,
    ) -> tuple[DelegateConfig | None, str | None]:
        """Find the configure tool call in the response, validate, return (cfg, err)."""
        for block in response.content:
            if (
                isinstance(block, (ToolUseBlock, BetaToolUseBlock))
                and block.name == CONFIGURE_TOOL_NAME
            ):
                try:
                    cfg = DelegateConfig.model_validate(dict(block.input or {}))
                except ValidationError as e:
                    return None, f"DelegateConfig validation failed: {e}"
                err = self._validate_coupling_rule(cfg, request)
                if err is not None:
                    return None, err
                return cfg, None
        return None, "configure tool was not called in the response"

    @staticmethod
    def _validate_coupling_rule(
        cfg: DelegateConfig,
        request: DelegateRequest,
    ) -> str | None:
        """Enforce the inherit_context ↔ system_prompt/tools coupling.

        - inherit_context=True ⇒ system_prompt MUST be None and tools MUST be None.
        - inherit_context=False ⇒ system_prompt MUST be set (cannot fall through
          to spine's system on a fresh-start delegate without explicit choice);
          tools MUST be set (an empty list is allowed if the delegate is purely
          finalize-shaped).
        Note: page creation is a delegate-internal tool call (create_page),
        not a side effect declared on DelegateConfig — so coupling-rule
        validation only checks system_prompt / tools / inherit_context.
        """
        if request.inherit_context:
            if cfg.system_prompt is not None:
                return (
                    "inherit_context=True requires system_prompt=null "
                    "(the inner loop reuses the spine's system for cache reuse). "
                    "Either set inherit_context=False or null the system_prompt."
                )
            if cfg.tools is not None:
                return (
                    "inherit_context=True requires tools=null "
                    "(the inner loop reuses the spine's full tool set for cache reuse). "
                    "Either set inherit_context=False or null the tools."
                )
        else:
            if cfg.system_prompt is None:
                return (
                    "inherit_context=False requires an explicit system_prompt "
                    "(ref or inline). Without inheritance, the inner loop has no system."
                )
            if cfg.tools is None:
                return (
                    "inherit_context=False requires an explicit tools list "
                    "(can be empty if the delegate only needs finalize)."
                )
        return None

    async def _run_inner_for_delegate(
        self,
        *,
        req: DelegateRequest,
        cfg: DelegateConfig,
        spine_messages: Sequence[dict],
        system_prompt: str,
        budget_clock: BudgetClock,
        call_id: str,
        delegate_id: str,
        sample_idx: int,
        trace: CallTrace,
    ) -> InnerLoopResult:
        """Build the inner loop's seed messages + tools, then run it."""
        finalize_schema = self._resolve_finalize_schema(cfg.finalize_schema)
        finalize_tool = build_finalize_tool(input_schema=finalize_schema)

        if req.inherit_context:
            inner_system = system_prompt
            inner_tools_named: list[str] = list(self.config.direct_tools)
            inner_direct_tools = resolve_direct_tools(inner_tools_named)
            inner_tools: list[Tool] = [
                # Same shape as mainline (delegate / configure / finalize / direct);
                # but for inner loops we replace mainline's default-schema finalize
                # with cfg's schema-set finalize, and we keep delegate/configure
                # so the inner agent can recurse via more delegates.
                *self._build_inner_loop_inherited_tools(
                    finalize_tool=finalize_tool, inner_direct_tools=inner_direct_tools
                ),
            ]
            framing = self._render_continuation_framing(req, cfg)
            seed_messages = [*spine_messages, {"role": "user", "content": framing}]
        else:
            inner_system = self._resolve_system_prompt(cfg.system_prompt)
            # The model often lists `finalize` in cfg.tools defensively
            # (it's the universal terminator and the configure description
            # tells it to call finalize). The orchestrator builds finalize
            # itself via build_finalize_tool with cfg.finalize_schema, so
            # silently filter `finalize` out of the registry-resolved list.
            tool_names = [n for n in (cfg.tools or []) if n != FINALIZE_TOOL_NAME]
            inner_direct_tools = resolve_direct_tools(tool_names)
            inner_tools = [finalize_tool, *inner_direct_tools]
            framing = self._render_isolation_framing(req, cfg)
            seed_messages = [{"role": "user", "content": framing}]

        await trace.record(
            AxonInnerLoopStartedEvent(
                delegate_id=delegate_id,
                sample_idx=sample_idx,
                inherit_context=req.inherit_context,
                tool_names=[t.name for t in inner_tools],
            )
        )
        inner_result = await run_inner_loop(
            system_prompt=inner_system,
            seed_messages=seed_messages,
            tools=inner_tools,
            model=self.config.main_model,
            model_config=None,
            db=self.db,
            call_id=call_id,
            phase=f"inner[{delegate_id}/{sample_idx}]",
            budget_clock=budget_clock,
            max_rounds=cfg.max_rounds,
        )
        await trace.record(
            AxonInnerLoopCompletedEvent(
                delegate_id=delegate_id,
                sample_idx=sample_idx,
                rounds=inner_result.rounds,
                cost_usd_used=budget_clock.cost_usd_used,
                finalized=inner_result.finalize_payload is not None,
                last_status=inner_result.stopped_because,
            )
        )
        return inner_result

    def _build_inner_loop_inherited_tools(
        self,
        *,
        finalize_tool: Tool,
        inner_direct_tools: list[Tool],
    ) -> list[Tool]:
        """For continuation regime: inner loop sees mainline's tool surface.

        The cache-prefix match requires the *exact same* tool_defs in the
        same order as the spine. We rebuild the mainline tool set, then
        swap the finalize tool with the cfg-schema-set version (same name,
        different input_schema) — this DOES change the cached tools
        prefix, so continuation-mode inner loops pay a one-time
        cache-miss on tools. See trade-off discussion in design notes.
        """
        from rumil.orchestrators.axon.tools import (
            build_configure_tool,
            build_delegate_tool,
        )

        return [
            build_delegate_tool(),
            build_configure_tool(),
            finalize_tool,
            *inner_direct_tools,
        ]

    def _resolve_system_prompt(self, spec: SystemPromptSpec | None) -> str:
        if spec is None:
            return ""
        if spec.inline is not None:
            return spec.inline
        if spec.ref is None:
            return ""
        prompt = self.config.system_prompt_registry.get(spec.ref)
        if prompt is None:
            raise _DelegateError(
                f"system_prompt ref {spec.ref!r} not in registry "
                f"(available: {sorted(self.config.system_prompt_registry)})"
            )
        return prompt

    def _resolve_finalize_schema(self, spec: FinalizeSchemaSpec) -> dict[str, Any]:
        if spec.inline is not None:
            return spec.inline
        if spec.ref is None:
            raise _DelegateError("FinalizeSchemaSpec must have ref or inline set")
        schema = self.config.finalize_schema_registry.get(spec.ref)
        if schema is None:
            raise _DelegateError(
                f"finalize_schema ref {spec.ref!r} not in registry "
                f"(available: {sorted(self.config.finalize_schema_registry)})"
            )
        return schema

    def _render_continuation_framing(
        self,
        req: DelegateRequest,
        cfg: DelegateConfig,
    ) -> list[dict]:
        """Continuation framing as a list of text blocks (matches spine shape)."""
        body = (
            "You are a delegate spawned by the main agent. You inherit the "
            "conversation above as your context.\n\n"
            f"Your task: {req.intent}\n\n"
            "Terminate by calling `finalize` with the schema described in "
            "your tool list. Your finalize result becomes the tool_result "
            "returned to the main agent for this delegate."
        )
        blocks: list[dict] = [{"type": "text", "text": body}]
        if cfg.extra_context and cfg.extra_context.strip():
            blocks.append({"type": "text", "text": cfg.extra_context.strip()})
        return blocks

    def _render_isolation_framing(
        self,
        req: DelegateRequest,
        cfg: DelegateConfig,
    ) -> list[dict]:
        """Isolation framing as a list of text blocks (matches spine shape)."""
        body = (
            f"Your task: {req.intent}\n\n"
            "Terminate by calling `finalize` with the schema described in "
            "your tool list. Your finalize result becomes the tool_result "
            "returned to your caller."
        )
        blocks: list[dict] = [{"type": "text", "text": body}]
        if cfg.extra_context and cfg.extra_context.strip():
            blocks.append({"type": "text", "text": cfg.extra_context.strip()})
        return blocks

    def _format_delegate_result(
        self,
        validated: dict[str, Any],
        artifact_key: str | None,
    ) -> str:
        body = json.dumps(validated, ensure_ascii=False, indent=2)
        if artifact_key:
            return f"[delegate result, persisted to artifact `{artifact_key}`]\n{body}"
        return body

    async def _call_mainline(
        self,
        *,
        system_prompt: str,
        messages: Sequence[dict],
        tool_defs: list[dict],
        call_id: str,
        phase: str,
        round_idx: int,
        budget_clock: BudgetClock,
    ) -> anthropic.types.Message:
        """One mainline (or configure-fork) API call. Records cost."""
        meta = LLMExchangeMetadata(
            call_id=call_id,
            phase=phase,
            round_num=round_idx,
        )
        api_resp = await call_anthropic_api(
            self.client,
            self.config.main_model,
            system_prompt,
            list(messages),
            tool_defs or None,
            metadata=meta,
            db=self.db,
            cache=True,
            model_config=None,
        )
        if api_resp.message.usage is not None:
            budget_clock.record_exchange(api_resp.message.usage, self.config.main_model)
        return api_resp.message

    @staticmethod
    def _budget_user_block(budget_clock: BudgetClock) -> dict:
        text = f"[budget] {budget_clock.render_for_prompt()}"
        if budget_clock.cost_exhausted:
            text += "\n[budget] Cost cap exhausted — finalize on your next turn."
        return {"type": "text", "text": text}

    @staticmethod
    def _tool_result_block(tool_use_id: str, content: str, is_error: bool) -> dict:
        block: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": content,
        }
        if is_error:
            block["is_error"] = True
        return block

    def _load_main_system_prompt(self) -> str:
        path = Path(self.config.main_system_prompt_path)
        if not path.is_absolute():
            here = Path(__file__).parent
            path = (here / path).resolve()
        return path.read_text(encoding="utf-8")

    async def _build_initial_user_message(
        self,
        inputs: OrchInputs,
        artifacts: ArtifactStore,
    ) -> list[dict]:
        """Render the spine's first user-role content block list.

        Sections: Question (always), Operating assumptions (if non-empty),
        Available pages (if seed_page_ids provided — truncated to
        AxonConfig.max_seed_pages with id+type+headline), Available
        artifacts (if any caller-seeded artifacts).
        """
        parts: list[dict] = [
            {
                "type": "text",
                "text": f"## Question\n\n{inputs.question}\n",
            },
        ]
        if OPERATING_ASSUMPTIONS_KEY in artifacts:
            parts.append(
                {
                    "type": "text",
                    "text": f"## Operating assumptions\n\n{inputs.operating_assumptions.strip()}\n",
                }
            )

        effective_seed_ids = await self._resolve_effective_seed_page_ids(inputs)
        seed_pages_text = await self._render_seed_pages_section(effective_seed_ids)
        if seed_pages_text:
            parts.append({"type": "text", "text": seed_pages_text})

        seed_announces = artifacts.announce_seed()
        if seed_announces:
            announce_text = "## Available artifacts\n\n" + "\n".join(seed_announces)
            parts.append({"type": "text", "text": announce_text})
        return parts

    async def _resolve_effective_seed_page_ids(
        self,
        inputs: OrchInputs,
    ) -> Sequence[str]:
        """Caller-supplied IDs take priority; otherwise auto-seed via embeddings.

        When ``OrchInputs.seed_page_ids`` is non-empty, returns it
        unchanged (truncation happens in the renderer). When it's empty
        and ``AxonConfig.auto_seed_from_question`` is on, embeds the
        question and pulls top-K similar pages from the workspace via
        :func:`search_pages_by_vector` (K = ``AxonConfig.max_seed_pages``).
        Returns the resolved page IDs in similarity-descending order.
        """
        if inputs.seed_page_ids:
            return inputs.seed_page_ids
        if not self.config.auto_seed_from_question:
            return ()
        from rumil.embeddings import embed_query, search_pages_by_vector

        try:
            embedding = await embed_query(inputs.question)
            ranked = await search_pages_by_vector(
                self.db,
                embedding,
                match_threshold=self.config.auto_seed_match_threshold,
                match_count=self.config.max_seed_pages,
            )
        except Exception as e:
            log.warning("axon: auto-seed embedding lookup failed: %s", e)
            return ()
        return [page.id for page, _score in ranked]

    async def _render_seed_pages_section(
        self,
        seed_page_ids: Sequence[str],
    ) -> str:
        """Build the '## Available pages' section text, or '' if none.

        Truncates the input list to ``AxonConfig.max_seed_pages``,
        fetches the pages in one batched call, and renders one line
        per page: ``- [<id>] <type>  "<headline>"``. Pages that don't
        resolve are listed as ``- [<id>] (not found)`` so a stale id
        in OrchInputs is loud.
        """
        if not seed_page_ids:
            return ""
        cap = self.config.max_seed_pages
        ids = list(seed_page_ids)
        if len(ids) > cap:
            log.warning(
                "axon: seed_page_ids has %d entries; truncating to AxonConfig.max_seed_pages=%d",
                len(ids),
                cap,
            )
            ids = ids[:cap]
        pages = await self.db.get_pages_by_ids(ids)
        lines: list[str] = ["## Available pages", ""]
        for pid in ids:
            page = pages.get(pid)
            if page is None:
                lines.append(f"- [{pid}] (not found)")
                continue
            headline = (page.headline or "").strip().replace("\n", " ")
            lines.append(f'- [{pid}] {page.page_type.value}  "{headline}"')
        lines.append("")
        lines.append("Use `load_page(<id>)` to read full content.")
        return "\n".join(lines) + "\n"


class _DelegateError(Exception):
    """Internal error raised when a delegate cannot be configured/run cleanly."""
