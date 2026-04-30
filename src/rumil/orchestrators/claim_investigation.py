"""
ClaimInvestigationOrchestrator: two-phase orchestrator for investigating claims.
"""

import asyncio
import logging
from collections.abc import Sequence

from rumil.available_calls import get_available_calls_preset
from rumil.calls.common import mark_call_completed
from rumil.calls.dispatches import (
    DISPATCH_DEFS,
    RECURSE_CLAIM_DISPATCH_DEF,
    RECURSE_DISPATCH_DEF,
    DispatchDef,
)
from rumil.calls.prioritization import run_prioritization_call
from rumil.constants import LAST_CALL_THRESHOLD, MIN_TWOPHASE_BUDGET
from rumil.context import build_prioritization_context
from rumil.database import DB
from rumil.llm import build_system_prompt
from rumil.models import (
    Call,
    CallType,
    Dispatch,
    LinkType,
    RecurseClaimDispatchPayload,
    RecurseDispatchPayload,
    Workspace,
)
from rumil.orchestrators.base import BaseOrchestrator
from rumil.orchestrators.common import (
    ClaimScore,
    PrioritizationResult,
    compute_priority_score,
    score_items_sequentially,
)
from rumil.tracing.broadcast import Broadcaster
from rumil.tracing.trace_events import (
    CallTypeFruitScoreItem,
    ClaimScoreItem,
    ContextBuiltEvent,
    DispatchesPlannedEvent,
    DispatchExecutedEvent,
    DispatchTraceItem,
    PhaseSkippedEvent,
    ScoringCompletedEvent,
)
from rumil.tracing.tracer import CallTrace, set_trace
from rumil.views import get_active_view

log = logging.getLogger(__name__)


class ClaimInvestigationOrchestrator(BaseOrchestrator):
    """Two-phase orchestrator for investigating claims.

    Phase 1: Fan out with claim-specific scouts (how-true, how-false,
    cruxes, relevant-evidence, stress-test-cases).
    Phase 2: Score identified items for impact and remaining fruit,
    then dispatch targeted follow-up (more scouts, find_considerations,
    recurse into claim or question investigation).
    """

    def __init__(
        self,
        db: DB,
        broadcaster: Broadcaster | None = None,
        budget_cap: int | None = None,
        pool_pre_registered: bool = False,
    ):
        super().__init__(db, broadcaster)
        self._invocation: int = 0
        self._call_id: str | None = None

        self._executed_since_last_plan: bool = False
        self._budget_cap: int | None = budget_cap
        self._consumed: int = 0
        self._initial_call: Call | None = None
        self._parent_call_id: str | None = None
        self._sequence_id: str | None = None
        self._seq_position: int = 0
        # See TwoPhaseOrchestrator for why this exists. When True, the
        # parent already registered our contribution via qbp_recurse, so
        # run() must not double-register.
        self._pool_pre_registered: bool = pool_pre_registered

    def _effective_budget(self, global_remaining: int) -> int:
        if self._budget_cap is not None:
            return min(global_remaining, self._budget_cap - self._consumed)
        return global_remaining

    async def _pacing_params(self) -> tuple[int, int]:
        if self._budget_cap is not None:
            return self._budget_cap, self._consumed
        return await self.db.get_budget()

    async def create_initial_call(
        self,
        claim_id: str,
        parent_call_id: str | None = None,
    ) -> str:
        """Eagerly create the phase-1 prioritization call record."""
        budget = self._effective_budget(await self.db.budget_remaining())
        budget = await self._paced_budget(budget)
        phase1_budget = budget
        p_call = await self.db.create_call(
            CallType.PRIORITIZATION,
            scope_page_id=claim_id,
            parent_call_id=parent_call_id,
            budget_allocated=phase1_budget,
            workspace=Workspace.PRIORITIZATION,
        )
        self._call_id = p_call.id
        self._initial_call = p_call
        self._parent_call_id = parent_call_id
        return p_call.id

    async def run(self, root_question_id: str) -> None:
        claim_id = root_question_id
        own_db = await self.db.fork()
        self.db = own_db
        await self._setup()
        remaining = await self.db.budget_remaining()
        effective = self._effective_budget(remaining)
        if effective < MIN_TWOPHASE_BUDGET:
            raise ValueError(
                "ClaimInvestigationOrchestrator requires a budget of at least "
                f"{MIN_TWOPHASE_BUDGET}, got {effective}"
            )
        if self._parent_call_id:
            seq = await self.db.create_call_sequence(
                parent_call_id=self._parent_call_id,
                scope_question_id=claim_id,
            )
            self._sequence_id = seq.id
            self._seq_position = 0
        self.pool_question_id = claim_id
        if not self._pool_pre_registered:
            contribution = self._budget_cap if self._budget_cap is not None else effective
            await self.db.qbp_register(claim_id, contribution)
        try:
            while True:
                remaining = await self.db.budget_remaining()
                pool = await self.db.qbp_get(claim_id)
                effective = min(self._effective_budget(remaining), pool.remaining)
                if effective <= 0:
                    break

                last_call = effective < LAST_CALL_THRESHOLD
                if last_call:
                    round_budget = effective
                else:
                    round_budget = await self._paced_budget(effective)
                result = await self.get_dispatches(
                    claim_id,
                    round_budget,
                    total_remaining=effective,
                    last_call=last_call,
                )
                if not result.dispatch_sequences and not result.children:
                    break

                results = await self.execute_dispatches(result, claim_id)
                if not results:
                    break
                if not any(not isinstance(r, Exception) for r in results):
                    break

                self._executed_since_last_plan = True

                if self._invocation > 1 or last_call:
                    view = get_active_view()
                    await view.refresh(
                        claim_id,
                        self.db,
                        parent_call_id=self._parent_call_id,
                        broadcaster=self.broadcaster,
                        force=True,
                        sequence_id=self._sequence_id,
                        sequence_position=self._seq_position,
                        pool_question_id=self.pool_question_id,
                    )
                    if self._sequence_id is not None:
                        self._seq_position += 1

                if last_call:
                    break
        finally:
            try:
                await self.db.qbp_unregister(claim_id)
            finally:
                await self._teardown()
                await own_db.close()

    async def _run_dispatch_sequence(
        self,
        sequence: Sequence[Dispatch],
        scope_question_id: str,
        parent_call_id: str | None,
        base_index: int,
        position_in_batch: int = 0,
    ) -> bool:
        result = await super()._run_dispatch_sequence(
            sequence,
            scope_question_id,
            parent_call_id,
            base_index,
            position_in_batch=position_in_batch,
        )
        if result:
            self._consumed += len(sequence)
        return result

    async def _is_new_claim(self, claim_id: str) -> bool:
        """A claim is 'new' if no other page depends on it yet."""
        links = await self.db.get_links_to(claim_id)
        return not any(l.link_type == LinkType.DEPENDS_ON for l in links)

    async def _cancel_initial_call(self) -> None:
        if self._initial_call is None:
            return
        call = self._initial_call
        self._initial_call = None
        if self._sequence_id is not None:
            call.sequence_id = self._sequence_id
            call.sequence_position = self._seq_position
            await self.db.save_call(call)
            self._seq_position += 1
        trace = CallTrace(call.id, self.db, broadcaster=self.broadcaster)
        set_trace(trace)
        await trace.record(
            PhaseSkippedEvent(
                phase="phase1",
                reason="Claim already has research.",
            )
        )
        await mark_call_completed(
            call,
            self.db,
            "Phase 1 skipped — claim already has research.",
        )

    async def get_dispatches(
        self,
        root_question_id: str,
        budget: int,
        *,
        parent_call_id: str | None = None,
        total_remaining: int | None = None,
        last_call: bool = False,
    ) -> "PrioritizationResult":
        claim_id = root_question_id

        if self._invocation == 0:
            self._invocation += 1
            if await self._is_new_claim(claim_id):
                return await self._phase1(
                    claim_id,
                    budget,
                    parent_call_id,
                    total_remaining=total_remaining,
                    last_call=last_call,
                )
            await self._cancel_initial_call()
            self._executed_since_last_plan = True

        if not self._executed_since_last_plan:
            return PrioritizationResult(dispatch_sequences=[])

        self._executed_since_last_plan = False
        self._invocation += 1
        return await self._phase2(
            claim_id,
            budget,
            self._parent_call_id,
            total_remaining=total_remaining,
            last_call=last_call,
        )

    async def _phase1(
        self,
        claim_id: str,
        budget: int,
        parent_call_id: str | None,
        total_remaining: int | None = None,
        last_call: bool = False,
    ) -> "PrioritizationResult":

        phase1_budget = budget
        log.info(
            "ClaimInvestigationOrchestrator phase1: claim=%s, budget=%d, phase1_budget=%d",
            claim_id[:8],
            budget,
            phase1_budget,
        )

        context_text, short_id_map = await build_prioritization_context(
            self.db,
            scope_question_id=claim_id,
            current_call_id=self._initial_call.id if self._initial_call else None,
        )
        if self._initial_call is not None:
            p_call = self._initial_call
            self._initial_call = None
            if self._sequence_id is not None:
                p_call.sequence_id = self._sequence_id
                p_call.sequence_position = self._seq_position
                await self.db.save_call(p_call)
                self._seq_position += 1
        else:
            p_call = await self.db.create_call(
                CallType.PRIORITIZATION,
                scope_page_id=claim_id,
                parent_call_id=parent_call_id,
                budget_allocated=phase1_budget,
                workspace=Workspace.PRIORITIZATION,
                sequence_id=self._sequence_id,
                sequence_position=self._seq_position if self._sequence_id else None,
            )
            if self._sequence_id is not None:
                self._seq_position += 1
        trace = CallTrace(p_call.id, self.db, broadcaster=self.broadcaster)
        set_trace(trace)
        await trace.record(ContextBuiltEvent(budget=phase1_budget))

        budget_line = (
            f"You have a budget of **{phase1_budget} research calls** to distribute "
            "among the dispatch tools below."
        )
        if last_call:
            budget_line += (
                " **This is your FINAL allocation — there will be no further "
                "research rounds after this. Spend the full budget on the "
                "highest-value work.**"
            )
        elif total_remaining is not None and total_remaining > phase1_budget:
            budget_line += (
                f" The overall question has **{total_remaining} budget remaining** "
                "across future rounds."
            )
        task = (
            f"{budget_line}\n\n"
            f"Scope claim ID: `{claim_id}`\n\n"
            "Your job is to call the dispatch tools to fan out exploratory research on "
            "this claim. All scout dispatches automatically target the scope claim. "
            "You MUST call at least one dispatch tool right now — this is "
            "your only turn and you will not get another chance. Distribute your budget "
            "among the scouting dispatch tools, weighting towards types that seem most "
            "useful for this claim and skipping types that are clearly irrelevant. "
            "Do not do anything else — just dispatch."
        )

        claim = await self.db.get_page(claim_id)
        embed_task = (
            f'the claim being investigated: "{claim.headline}"\n\n'
            "fan-out scouting prioritization for claim investigation."
            if claim
            else "fan-out scouting prioritization for claim investigation."
        )
        result = await run_prioritization_call(
            task,
            context_text,
            p_call,
            self.db,
            short_id_map=short_id_map,
            dispatch_types=list(get_available_calls_preset().claim_phase1_scouts),
            system_prompt=build_system_prompt(
                "claim_investigation_p1",
                task=embed_task,
                include_citations=False,
                include_per_call=False,
            ),
            prompt_name="claim_investigation_p1",
        )

        dispatches = list(result.dispatches)
        if not dispatches:
            log.warning(
                "Phase 1 produced no dispatches, synthesizing default scouts for claim=%s",
                claim_id[:8],
            )
            preset = get_available_calls_preset()
            for ct in preset.claim_phase1_scouts[:phase1_budget]:
                ddef = DISPATCH_DEFS[ct]
                dispatches.append(
                    Dispatch(
                        call_type=ct,
                        payload=ddef.schema(
                            question_id=claim_id,
                            reason="fallback — phase 1 produced no dispatches",
                        ),
                    )
                )
        sequences: list[list[Dispatch]] = [[d] for d in dispatches]

        await trace.record(
            DispatchesPlannedEvent(
                dispatches=[
                    DispatchTraceItem(
                        call_type=d.call_type.value,
                        **d.payload.model_dump(exclude_defaults=True),
                    )
                    for d in dispatches
                ],
            )
        )

        await mark_call_completed(
            p_call,
            self.db,
            f"Phase 1 complete. Planned {len(sequences)} concurrent sequences.",
        )

        self._call_id = p_call.id

        log.info(
            "ClaimInvestigationOrchestrator phase1 complete: %d sequences",
            len(sequences),
        )
        return PrioritizationResult(
            dispatch_sequences=sequences,
            call_id=p_call.id,
        )

    async def _phase2(
        self,
        claim_id: str,
        budget: int,
        parent_call_id: str | None,
        total_remaining: int | None = None,
        last_call: bool = False,
    ) -> "PrioritizationResult":
        from rumil.orchestrators.common import PrioritizationResult
        from rumil.orchestrators.two_phase import TwoPhaseOrchestrator

        log.info(
            "ClaimInvestigationOrchestrator phase2: claim=%s, budget=%d, last_call=%s",
            claim_id[:8],
            budget,
            last_call,
        )

        p_call = await self.db.create_call(
            CallType.PRIORITIZATION,
            scope_page_id=claim_id,
            parent_call_id=parent_call_id,
            budget_allocated=budget,
            workspace=Workspace.PRIORITIZATION,
            sequence_id=self._sequence_id,
            sequence_position=self._seq_position if self._sequence_id else None,
        )
        if self._sequence_id is not None:
            self._seq_position += 1
        trace = CallTrace(p_call.id, self.db, broadcaster=self.broadcaster)
        set_trace(trace)
        await trace.record(ContextBuiltEvent(budget=budget))

        scope_page = await self.db.get_page(claim_id)
        if not scope_page:
            raise RuntimeError(f"Scope claim {claim_id} not found.")

        scope_judgements = await self.db.get_judgements_for_question(claim_id)
        scope_judgement = (
            max(scope_judgements, key=lambda j: j.created_at) if scope_judgements else None
        )

        dependent_pages = [page for page, _link in await self.db.get_dependents(claim_id)]
        child_questions = await self.db.get_child_questions(claim_id)
        all_items = dependent_pages + list(child_questions)

        scoring_tasks: list = []
        scoring_tasks.append(
            score_items_sequentially(
                parent_page=scope_page,
                parent_judgement=scope_judgement,
                items=all_items,
                system_prompt_name="score_claim_items",
                response_model=ClaimScore,
                call_id=p_call.id,
                db=self.db,
            )
        )

        scoring_tasks.append(self.db.get_latest_scout_fruit(claim_id))

        scoring_results = await asyncio.gather(*scoring_tasks)
        item_scores: list[dict] = scoring_results[0]
        scout_fruit: dict[str, int | None] = scoring_results[1]

        await trace.record(
            ScoringCompletedEvent(
                claim_scores=[ClaimScoreItem(**s) for s in item_scores],
                per_type_fruit=[
                    CallTypeFruitScoreItem(call_type=ct, fruit=f or 0, reasoning="")
                    for ct, f in scout_fruit.items()
                ],
            )
        )

        scores_text = ""
        if item_scores:
            lines = ["## Item Scores", ""]
            for s in item_scores:
                pid = s.get("page_id", s.get("question_id", "?"))
                priority = compute_priority_score(
                    s.get("impact_on_question", 0),
                    s.get("broader_impact", 0),
                    s.get("fruit", 0),
                )
                lines.append(
                    f"- `{pid}` — {s.get('headline', '')}: "
                    f"impact_on_q={s.get('impact_on_question', 0)}, "
                    f"broader={s.get('broader_impact', 0)}, "
                    f"fruit={s.get('fruit', 0)}, "
                    f"**priority={priority}** "
                    f"({s.get('reasoning', '')})"
                )
            lines.append("")
            scores_text = "\n".join(lines)

        if scout_fruit:
            fruit_lines = ["## Per-Scout-Type Remaining Fruit (from latest calls)", ""]
            for ct, f in sorted(scout_fruit.items()):
                fruit_lines.append(
                    f"- **{ct}**: {f}/10" if f is not None else f"- **{ct}**: unknown"
                )
            fruit_lines.append("")
            scores_text += "\n".join(fruit_lines)

        # Take the authoritative pool snapshot here — between this point and
        # the LLM dispatching, peer cycles can only consume more from the pool.
        # Using a fresh snapshot ensures the budget line and the Coordination
        # context section agree on what's available.
        # Skip the bailout when the pool was never registered (e.g. callers
        # that invoke get_dispatches outside the run() loop, like
        # scripts/run_prio.py): "no pool" must not look like "drained to zero".
        fresh_pool = await self.db.qbp_get(claim_id)
        if fresh_pool.registered:
            budget = min(budget, max(fresh_pool.remaining, 0))
            # If the pool drained between top-of-loop and now (i.e. peer cycles
            # consumed our slice), bail before calling the LLM with budget 0.
            if budget <= 0:
                await mark_call_completed(
                    p_call,
                    self.db,
                    "Pool drained by peer cycles before this round could plan.",
                )
                return PrioritizationResult(dispatch_sequences=[], call_id=p_call.id)
        context_text, short_id_map = await build_prioritization_context(
            self.db,
            scope_question_id=claim_id,
            current_call_id=p_call.id,
        )
        budget_line = f"You have a budget of **{budget} budget units** to allocate."
        if last_call:
            budget_line += (
                " **This is your FINAL allocation — there will be no further "
                "research rounds after this. Spend the full budget on the "
                "highest-value remaining work.**"
            )
        elif total_remaining is not None and total_remaining > budget:
            budget_line += (
                f" The overall question has **{total_remaining} budget remaining** "
                "across future rounds."
            )
        ingest_hint = ""
        if self.ingest_hint:
            ingest_hint = f"\n\n**Note:** {self.ingest_hint}"
            self.ingest_hint = ""

        task = (
            f"{budget_line}\n\n"
            f"Scope claim ID: `{claim_id}`\n\n"
            f"{scores_text}\n\n"
            "You must make all your dispatch calls now — this is your only turn. "
            f"Each recurse call must have a budget of at least {MIN_TWOPHASE_BUDGET}."
            f"{ingest_hint}"
        )

        extra_defs: list[DispatchDef] = []
        if budget >= MIN_TWOPHASE_BUDGET:
            extra_defs.append(RECURSE_CLAIM_DISPATCH_DEF)
            extra_defs.append(RECURSE_DISPATCH_DEF)

        claim = await self.db.get_page(claim_id)
        embed_task = (
            f'the claim being investigated: "{claim.headline}"\n\n'
            "main-phase prioritization across open lines of claim investigation."
            if claim
            else "main-phase prioritization for claim investigation."
        )
        result = await run_prioritization_call(
            task,
            context_text,
            p_call,
            self.db,
            short_id_map=short_id_map,
            dispatch_types=list(get_available_calls_preset().claim_phase2_dispatch),
            extra_dispatch_defs=extra_defs or None,
            system_prompt=build_system_prompt(
                "claim_investigation_p2",
                task=embed_task,
                include_citations=False,
                include_per_call=False,
            ),
            prompt_name="claim_investigation_p2",
            dispatch_budget=budget,
        )

        sequences: list[list[Dispatch]] = []
        children: list[tuple[ClaimInvestigationOrchestrator | TwoPhaseOrchestrator, str]] = []
        for d in result.dispatches:
            if isinstance(d.payload, RecurseClaimDispatchPayload):
                resolved = await self.db.resolve_page_id(d.payload.question_id)
                if not resolved:
                    log.warning(
                        "Recurse claim ID not found: %s",
                        d.payload.question_id[:8],
                    )
                    continue
                await self.db.qbp_recurse(claim_id, resolved, d.payload.budget)
                child = ClaimInvestigationOrchestrator(
                    self.db,
                    self.broadcaster,
                    budget_cap=d.payload.budget,
                    pool_pre_registered=True,
                )
                child._parent_call_id = p_call.id
                children.append((child, resolved))
                log.info(
                    "Queued recursive claim investigation: claim=%s, budget=%d — %s",
                    resolved[:8],
                    d.payload.budget,
                    d.payload.reason,
                )
            elif isinstance(d.payload, RecurseDispatchPayload):
                resolved = await self.db.resolve_page_id(d.payload.question_id)
                if not resolved:
                    log.warning(
                        "Recurse question ID not found: %s",
                        d.payload.question_id[:8],
                    )
                    continue
                await self.db.qbp_recurse(claim_id, resolved, d.payload.budget)
                child = TwoPhaseOrchestrator(
                    self.db,
                    self.broadcaster,
                    budget_cap=d.payload.budget,
                    pool_pre_registered=True,
                )
                child._parent_call_id = p_call.id
                children.append((child, resolved))
                log.info(
                    "Queued recursive question investigation: question=%s, budget=%d — %s",
                    resolved[:8],
                    d.payload.budget,
                    d.payload.reason,
                )
            else:
                sequences.append([d])

        all_dispatches = [d for seq in sequences for d in seq]
        all_trace_items = [
            DispatchTraceItem(
                call_type=d.call_type.value,
                **d.payload.model_dump(exclude_defaults=True),
            )
            for d in all_dispatches
        ]
        for d in result.dispatches:
            if isinstance(d.payload, (RecurseClaimDispatchPayload, RecurseDispatchPayload)):
                all_trace_items.append(
                    DispatchTraceItem(
                        call_type="recurse",
                        **d.payload.model_dump(exclude_defaults=True),
                    )
                )
        await trace.record(DispatchesPlannedEvent(dispatches=all_trace_items))

        recurse_base = len(all_dispatches)
        child_pages = await self.db.get_pages_by_ids([child_id for _, child_id in children])
        for ci, (child, child_id) in enumerate(children):
            child_call_id = await child.create_initial_call(
                child_id,
                parent_call_id=p_call.id,
            )
            child_page = child_pages.get(child_id)
            await trace.record(
                DispatchExecutedEvent(
                    index=recurse_base + ci,
                    child_call_type="recurse",
                    question_id=child_id,
                    question_headline=child_page.headline if child_page else "",
                    child_call_id=child_call_id,
                )
            )

        await mark_call_completed(
            p_call,
            self.db,
            f"Phase 2 complete. Planned {len(sequences)} concurrent sequences, "
            f"{len(children)} recursive children.",
        )

        self._call_id = p_call.id

        log.info(
            "ClaimInvestigationOrchestrator phase2 complete: %d sequences, %d children",
            len(sequences),
            len(children),
        )
        return PrioritizationResult(
            dispatch_sequences=sequences,
            call_id=p_call.id,
            children=children,
        )
