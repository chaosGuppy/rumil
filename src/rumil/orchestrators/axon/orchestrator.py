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

Implements the core flow for n=1 and n>1 delegates with the
``write_artifact`` side effect. Workspace-page creation happens
inside delegates via the ``create_page`` tool, not as a side effect.
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
    AxonConfig,
    OrchInputs,
    OrchResult,
    build_initial_artifacts,
)
from rumil.orchestrators.axon.direct_tools import (
    DirectToolCtx,
    reset_direct_tool_ctx,
    set_direct_tool_ctx,
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
    AxonAutoSeedFailedEvent,
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
from rumil.tracing import get_langfuse, observe, phase_span, propagate_attributes
from rumil.tracing.broadcast import Broadcaster
from rumil.tracing.tracer import CallTrace, reset_trace, set_trace

log = logging.getLogger(__name__)

_HARD_MAX_ROUNDS_FALLBACK = 50
_MAX_CONFIGURE_RETRIES = 2
_CONFIGURE_PLACEHOLDER = "[awaiting configure]"


def _known_server_tool_def(name: str) -> dict | None:
    """Return the Anthropic server-tool def for a known name, or None.

    Server tools are executed Anthropic-side (no fn dispatch); they
    travel in the API request's ``tools`` list alongside our regular
    tool defs. Configure can list them by name in ``cfg.tools``; the
    orchestrator routes them here instead of through the direct-tool
    registry.
    """
    if name == "web_search":
        return {
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": 5,
        }
    return None


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

    def _build_compaction_kwargs(self) -> dict[str, Any]:
        """Server-side compaction (compact_20260112) kwargs for call_anthropic_api.

        Mirrors :class:`simple_spine.SimpleSpineOrchestrator`: when
        ``enable_server_compaction`` is on, configure the API to summarise
        the prefix once it crosses ``compaction_trigger_tokens`` and drop
        every prior message on subsequent turns. The system prompt is
        preserved (cached separately); the original first user message is
        NOT preserved automatically — the configured instructions prompt
        tells the summariser what to keep.

        Returns ``{}`` when compaction is disabled. Reused across mainline
        rounds, configure follow-ups, and inner-loop calls so any
        long-running thread benefits.
        """
        if not self.config.enable_server_compaction:
            return {}
        edit: dict[str, Any] = {
            "type": "compact_20260112",
            "trigger": {
                "type": "input_tokens",
                "value": self.config.compaction_trigger_tokens,
            },
        }
        instructions = self._load_compaction_instructions()
        if instructions:
            edit["instructions"] = instructions
        return {
            "context_management": {"edits": [edit]},
            "betas": ["compact-2026-01-12"],
        }

    def _load_compaction_instructions(self) -> str:
        """Read the compaction instructions prompt, if configured."""
        path_raw = self.config.compaction_instructions_path
        if path_raw is None:
            return ""
        path = Path(path_raw)
        if not path.is_absolute():
            here = Path(__file__).parent
            path = (here / path).resolve()
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            log.warning("axon: compaction_instructions_path missing: %s", path)
            return ""

    @observe(name="orchestrator.axon")
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

        lf = get_langfuse()
        if lf is not None:
            lf.update_current_span(
                name=f"orchestrator.axon[{self.config.name}]",
                metadata={
                    "call_id": call_id,
                    "run_id": run_id,
                    "call_type": call_type.value,
                    "parent_call_id": parent_call_id,
                    "config_name": self.config.name,
                    "main_model": self.config.main_model,
                    "budget_usd": inputs.budget_usd,
                    "max_seed_pages": self.config.max_seed_pages,
                    "auto_seed_from_question": self.config.auto_seed_from_question,
                    "enable_server_compaction": self.config.enable_server_compaction,
                    "question_excerpt": inputs.question[:240],
                    "seed_page_id_count": len(inputs.seed_page_ids),
                },
            )

        trace = CallTrace(call_id=call_id, db=self.db, broadcaster=self.broadcaster)
        token = set_trace(trace)
        try:
            with propagate_attributes(
                session_id=run_id,
                metadata={
                    "orchestrator": "axon",
                    "config_name": self.config.name,
                    "call_id": call_id,
                },
                tags=["orchestrator:axon", f"axon_config:{self.config.name}"],
            ):
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
        artifacts = ArtifactStore(seed=build_initial_artifacts(inputs, self.config.artifact_seeds))

        # Scope a DirectToolCtx with the run's ArtifactStore so the
        # mainline read_artifact / load_page / create_page tool fns
        # have what they need. Overrides any externally-set ctx for the
        # duration of the run; that's fine — orchestrator state is
        # the authoritative source for these fields.
        direct_ctx = DirectToolCtx(
            db=self.db,
            call_id=call_id,
            artifacts=artifacts,
        )
        direct_token = set_direct_tool_ctx(direct_ctx)
        try:
            return await self._run_inner_with_ctx(
                inputs=inputs,
                call=call,
                call_id=call_id,
                run_id=run_id,
                trace=trace,
                budget_clock=budget_clock,
                artifacts=artifacts,
            )
        finally:
            reset_direct_tool_ctx(direct_token)

    async def _run_inner_with_ctx(
        self,
        *,
        inputs: OrchInputs,
        call: Call,
        call_id: str,
        run_id: str,
        trace: CallTrace,
        budget_clock: BudgetClock,
        artifacts: ArtifactStore,
    ) -> OrchResult:
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
        first_user_text = await self._build_initial_user_message(inputs, artifacts, trace)
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
            with phase_span(f"round_{round_idx}"):
                with phase_span("mainline"):
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

                finalize_block = next(
                    (tu for tu in tool_uses if tu.name == FINALIZE_TOOL_NAME), None
                )
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
        # Configure-fork messages need placeholder tool_results for every
        # tool_use in the assistant turn (delegates AND any peer direct
        # tools / unexpected calls) — the API rejects fork messages
        # whose prior tool_use blocks lack matching tool_results.
        peer_tool_use_ids = tuple(
            tu.id
            for tu in tool_uses
            if tu.name != DELEGATE_TOOL_NAME
            and tu.id not in {pd.tool_use_id for pd in delegate_calls}
        )
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
                    peer_tool_use_ids=peer_tool_use_ids,
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

    @observe(name="delegate")
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
        peer_tool_use_ids: Sequence[str] = (),
    ) -> _DelegateOutcome:
        """Run configure → inner loop → side effects for one delegate."""
        delegate_id = uuid.uuid4().hex[:8]
        req = pending.request
        lf = get_langfuse()
        if lf is not None:
            lf.update_current_span(
                name=f"delegate[{delegate_id}]",
                metadata={
                    "delegate_id": delegate_id,
                    "round_idx": round_idx,
                    "tool_use_id": pending.tool_use_id,
                    "intent_excerpt": req.intent[:200],
                    "inherit_context": req.inherit_context,
                    "budget_usd": req.budget_usd,
                    "n": req.n,
                    "regime": "continuation" if req.inherit_context else "isolation",
                },
            )
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
                artifacts=artifacts,
                peer_tool_use_ids=peer_tool_use_ids,
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
                    artifacts=artifacts,
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
        artifacts: ArtifactStore,
        peer_tool_use_ids: Sequence[str] = (),
    ) -> DelegateConfig:
        """Run the configure follow-up call(s) until a valid DelegateConfig lands.

        Bounded retry on coupling-rule violations: append a corrective
        user message and re-fire. After ``_MAX_CONFIGURE_RETRIES`` we
        give up and surface an error.
        """
        corrective: str | None = None
        for attempt in range(_MAX_CONFIGURE_RETRIES + 1):
            with phase_span(f"configure[{delegate_id}/attempt_{attempt}]"):
                cfg = await self._configure_attempt(
                    pending=pending,
                    all_pending=all_pending,
                    spine_messages=spine_messages,
                    system_prompt=system_prompt,
                    mainline_tool_defs=mainline_tool_defs,
                    budget_clock=budget_clock,
                    call_id=call_id,
                    trace=trace,
                    delegate_id=delegate_id,
                    round_idx=round_idx,
                    artifacts=artifacts,
                    peer_tool_use_ids=peer_tool_use_ids,
                    corrective=corrective,
                )
            if isinstance(cfg, DelegateConfig):
                return cfg
            corrective = cfg  # err string for next attempt's corrective
            await trace.record(
                AxonConfigureRetriedEvent(
                    delegate_id=delegate_id,
                    attempt=attempt + 1,
                    reason=corrective or "unknown",
                )
            )
        raise _DelegateError(
            f"configure follow-up failed after {_MAX_CONFIGURE_RETRIES + 1} attempts: {corrective}"
        )

    async def _configure_attempt(
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
        artifacts: ArtifactStore,
        peer_tool_use_ids: Sequence[str],
        corrective: str | None,
    ) -> DelegateConfig | str:
        """Run one configure follow-up call. Returns the cfg on success or an err string for retry."""
        fork_messages = self._build_configure_fork_messages(
            spine_messages=spine_messages,
            all_pending=all_pending,
            target=pending,
            corrective=corrective,
            peer_tool_use_ids=peer_tool_use_ids,
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
        if cfg is None:
            return err or "configure tool was not called in the response"

        # Validate artifact_keys after coupling rule passes; missing
        # keys go through the same corrective-retry path so the model
        # can self-correct typos.
        missing = artifacts.require_keys(cfg.artifact_keys)
        if missing:
            return (
                f"artifact_keys references unknown key(s) {missing}. "
                f"Available keys: {artifacts.list_keys()}. "
                "Pick existing keys or drop the field."
            )

        # Validate cfg.tools — names must be either the universal
        # `finalize`, a known server tool, or a registered direct
        # tool. Surface unknown names as corrective rather than
        # letting them crash later.
        bad_tools = self._unknown_tool_names(cfg.tools or [])
        if bad_tools:
            from rumil.orchestrators.axon.tools import list_direct_tool_names

            return (
                f"tools references unknown name(s) {bad_tools}. "
                f"Allowed: registered direct tools {list_direct_tool_names()} "
                "+ the server tool `web_search` + the universal `finalize` "
                "(which is auto-added so you can drop it). "
                "Names like `web_research` or `workspace_lookup` are "
                "*system_prompt* refs, not tool names — use them under "
                '`system_prompt: {ref: "..."}` instead.'
            )

        await trace.record(
            AxonConfigurePreparedEvent(
                delegate_id=delegate_id,
                config=cfg.model_dump(),
                rationale=cfg.rationale,
                cost_usd_used=budget_clock.cost_usd_used,
            )
        )
        return cfg

    def _build_configure_fork_messages(
        self,
        *,
        spine_messages: Sequence[dict],
        all_pending: Sequence[_PendingDelegate],
        target: _PendingDelegate,
        corrective: str | None,
        peer_tool_use_ids: Sequence[str] = (),
    ) -> list[dict]:
        """Build the message stack for one configure follow-up call.

        Spine prefix + a single user-role block containing placeholder
        tool_results (one per parallel delegate, identical text for
        cache uniformity, plus placeholders for any non-delegate peer
        tool_uses on the same assistant turn — load_page / read_artifact
        / etc. — so the API doesn't reject the fork for unmatched
        tool_use ids) + a directive identifying the target + optional
        corrective on retry.
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
        for tool_use_id in peer_tool_use_ids:
            placeholders.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
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
            "If inherit_context=True: leave system_prompt and tools as null (the delegate reuses my system + tools — that's the cache-shared continuation).",
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
    def _unknown_tool_names(names: Sequence[str]) -> list[str]:
        """Return tool names that don't resolve to anything we can run.

        Valid names: registered direct tools (load_page, read_artifact,
        create_page, ...), the `finalize` token (auto-added by the
        orchestrator; harmless to list), and known server tool names
        (web_search). Anything else is returned for the corrective
        retry path.
        """
        from rumil.orchestrators.axon.tools import list_direct_tool_names

        valid = set(list_direct_tool_names()) | {FINALIZE_TOOL_NAME}
        return [n for n in names if n not in valid and _known_server_tool_def(n) is None]

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
                    "(the delegate reuses the spine's system for cache reuse). "
                    "Either set inherit_context=False or null the system_prompt."
                )
            if cfg.tools is not None:
                return (
                    "inherit_context=True requires tools=null "
                    "(the delegate reuses the spine's full tool set for cache reuse). "
                    "Either set inherit_context=False or null the tools."
                )
        else:
            if cfg.system_prompt is None:
                return (
                    "inherit_context=False requires an explicit system_prompt "
                    "(ref or inline). Without inheritance, the delegate has no system."
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
        artifacts: ArtifactStore,
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
            framing = self._render_continuation_framing(req, cfg, artifacts)
            seed_messages = [*spine_messages, {"role": "user", "content": framing}]
        else:
            inner_system = self._resolve_system_prompt(cfg.system_prompt, artifacts)
            # Split cfg.tools into:
            # - finalize: filtered out (orchestrator builds it separately)
            # - known server tools (web_search): routed via server_tool_defs
            # - everything else: resolved through the direct-tool registry
            regular_names: list[str] = []
            for n in cfg.tools or []:
                if n == FINALIZE_TOOL_NAME:
                    continue
                if _known_server_tool_def(n) is not None:
                    continue
                regular_names.append(n)
            inner_direct_tools = resolve_direct_tools(regular_names)
            inner_tools = [finalize_tool, *inner_direct_tools]
            framing = self._render_isolation_framing(req, cfg, artifacts)
            seed_messages = [{"role": "user", "content": framing}]

        # Build server-tool defs from any known server-tool names in
        # cfg.tools (currently just web_search).
        server_tool_defs: list[dict] = []
        for n in cfg.tools or []:
            stdef = _known_server_tool_def(n)
            if stdef is not None:
                server_tool_defs.append(stdef)

        await trace.record(
            AxonInnerLoopStartedEvent(
                delegate_id=delegate_id,
                sample_idx=sample_idx,
                inherit_context=req.inherit_context,
                tool_names=[t.name for t in inner_tools],
            )
        )
        compaction_kwargs = self._build_compaction_kwargs()
        with phase_span(f"inner[{delegate_id}/{sample_idx}]"):
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
                context_management=compaction_kwargs.get("context_management"),
                betas=compaction_kwargs.get("betas"),
                server_tool_defs=server_tool_defs,
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

    def _resolve_system_prompt(
        self,
        spec: SystemPromptSpec | None,
        artifacts: ArtifactStore,
    ) -> str:
        """Resolve a SystemPromptSpec to its actual text.

        ``ref`` is now an artifact key — config-time prompts are seeded
        as artifacts (via :class:`AxonConfig.artifact_seeds`) so the
        spine and delegates use the same store. Mid-run delegates can
        write a prompt-shaped artifact via ``write_artifact`` and a
        sibling delegate's configure can reference its key.
        """
        if spec is None:
            return ""
        if spec.inline is not None:
            return spec.inline
        if spec.ref is None:
            return ""
        art = artifacts.get(spec.ref)
        if art is None:
            raise _DelegateError(
                f"system_prompt ref {spec.ref!r} not in artifact store "
                f"(available: {artifacts.list_keys()})"
            )
        return art.text

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
        artifacts: ArtifactStore,
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
        artifact_block = self._render_artifact_block(cfg, artifacts)
        if artifact_block:
            blocks.append({"type": "text", "text": artifact_block})
        if cfg.extra_context and cfg.extra_context.strip():
            blocks.append({"type": "text", "text": cfg.extra_context.strip()})
        return blocks

    def _render_isolation_framing(
        self,
        req: DelegateRequest,
        cfg: DelegateConfig,
        artifacts: ArtifactStore,
    ) -> list[dict]:
        """Isolation framing as a list of text blocks (matches spine shape)."""
        body = (
            f"Your task: {req.intent}\n\n"
            "Terminate by calling `finalize` with the schema described in "
            "your tool list. Your finalize result becomes the tool_result "
            "returned to your caller."
        )
        blocks: list[dict] = [{"type": "text", "text": body}]
        artifact_block = self._render_artifact_block(cfg, artifacts)
        if artifact_block:
            blocks.append({"type": "text", "text": artifact_block})
        if cfg.extra_context and cfg.extra_context.strip():
            blocks.append({"type": "text", "text": cfg.extra_context.strip()})
        return blocks

    @staticmethod
    def _render_artifact_block(
        cfg: DelegateConfig,
        artifacts: ArtifactStore,
    ) -> str:
        """Render configure-selected artifact_keys as an XML-fenced block.

        Returns ``""`` when ``cfg.artifact_keys`` is empty. Missing keys
        are validated upstream in :meth:`_validate_coupling_rule_with_artifacts`
        — by the time we reach here all keys are present in the store.
        """
        if not cfg.artifact_keys:
            return ""
        return artifacts.render_block(cfg.artifact_keys)

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
            **self._build_compaction_kwargs(),
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
        base = path.read_text(encoding="utf-8")
        registry = self._build_registry_summary()
        if registry:
            return base.rstrip() + "\n\n" + registry
        return base

    def _build_registry_summary(self) -> str:
        """Append registry contents to the spine system prompt.

        Two surfaces the model needs to know about for valid configure
        outputs:

        - Direct-tool names + known server tools: usable in
          ``cfg.tools``.
        - ``finalize_schema_registry`` keys: usable as
          ``finalize_schema: {ref: "<name>"}`` for any delegate.

        System prompts (formerly a separate registry) live as artifact
        seeds now — they appear in the spine's first user message
        under "## Available artifacts" with their author-supplied
        descriptions, and ``system_prompt: {ref: "<key>"}`` resolves
        against the ArtifactStore.
        """
        from rumil.orchestrators.axon.tools import list_direct_tool_names

        fin_keys = sorted(self.config.finalize_schema_registry)
        tool_names = list_direct_tool_names()
        if not fin_keys and not tool_names:
            return ""
        lines: list[str] = ["## Available registries (use in `configure` calls)"]
        if tool_names:
            lines.append("")
            lines.append("### Tools for `cfg.tools` (isolation delegates):")
            for n in tool_names:
                lines.append(f"- `{n}`")
            lines.append(
                "- `web_search` — Anthropic server tool; routes via "
                "context-management. Pair with a `system_prompt` that "
                "instructs the delegated agent on search strategy."
            )
            lines.append(
                "(`finalize` is auto-added per delegate; you can list "
                'it harmlessly. For `system_prompt: {ref: "..."}`, the '
                "ref is an artifact key — see '## Available artifacts' "
                "in your first user message for the keys that already "
                "have prompt content.)"
            )
        if fin_keys:
            lines.append("")
            lines.append('### Finalize schemas (`finalize_schema: {ref: "<name>"}`):')
            for k in fin_keys:
                schema = self.config.finalize_schema_registry[k]
                desc = self._summarise_finalize_schema(schema)
                lines.append(f"- `{k}`: {desc}")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _summarise_finalize_schema(schema: dict[str, Any]) -> str:
        """Produce a one-line summary of a finalize schema for the registry list."""
        desc = schema.get("description")
        if isinstance(desc, str) and desc.strip():
            return desc.strip()[:200]
        required = schema.get("required") or []
        properties = schema.get("properties") or {}
        prop_pairs: list[str] = []
        for name, spec in properties.items():
            mark = "" if name in required else "?"
            type_hint = spec.get("type", "?") if isinstance(spec, dict) else "?"
            prop_pairs.append(f"{name}{mark}:{type_hint}")
        return "{" + ", ".join(prop_pairs) + "}"

    async def _build_initial_user_message(
        self,
        inputs: OrchInputs,
        artifacts: ArtifactStore,
        trace: CallTrace | None = None,
    ) -> list[dict]:
        """Render the spine's first user-role content block list.

        Sections (each only if applicable): Question, Available pages
        (id + type + headline for seed_page_ids), Available artifacts
        (announcements with description + load-mode hint), Inline
        artifact bodies (XML-fenced for any caller seed flagged
        ``render_inline=True`` — operating_assumptions defaults to
        inline). Operating assumptions render via the artifact path now;
        no separate "## Operating assumptions" section.
        """
        parts: list[dict] = [
            {
                "type": "text",
                "text": f"## Question\n\n{inputs.question}\n",
            },
        ]

        effective_seed_ids = await self._resolve_effective_seed_page_ids(inputs, trace)
        seed_pages_text = await self._render_seed_pages_section(effective_seed_ids)
        if seed_pages_text:
            parts.append({"type": "text", "text": seed_pages_text})

        seed_announces = artifacts.announce_seed()
        if seed_announces:
            announce_text = "## Available artifacts\n\n" + "\n".join(
                f"- {line}" for line in seed_announces
            )
            parts.append({"type": "text", "text": announce_text})

        inline_block = artifacts.render_seed_inline_block()
        if inline_block:
            parts.append({"type": "text", "text": inline_block})

        return parts

    async def _resolve_effective_seed_page_ids(
        self,
        inputs: OrchInputs,
        trace: CallTrace | None = None,
    ) -> Sequence[str]:
        """Caller-supplied IDs take priority; otherwise auto-seed via embeddings.

        When ``OrchInputs.seed_page_ids`` is non-empty, returns it
        unchanged (truncation happens in the renderer). When it's empty
        and ``AxonConfig.auto_seed_from_question`` is on, embeds the
        question and pulls top-K similar pages from the workspace via
        :func:`search_pages_by_vector` (K = ``AxonConfig.max_seed_pages``).
        Returns the resolved page IDs in similarity-descending order.

        If the embedding lookup raises (flaky service, RPC error, etc.)
        the run continues with no seed pages but emits an
        :class:`AxonAutoSeedFailedEvent` so the failure is visible in
        the trace UI.
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
            if trace is not None:
                await trace.record(
                    AxonAutoSeedFailedEvent(
                        reason=f"{type(e).__name__}: {e}",
                        question_excerpt=inputs.question[:200],
                    )
                )
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
