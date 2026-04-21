"""QuestionPrioritiser: actor-model driver for two-phase question investigation.

Owns the round loop for a single question under the shared registry.
The V1 facade (``TwoPhaseOrchestrator``) is now a thin shell that
attaches DB/broadcaster/budget-cap, transfers the full budget, and
awaits completion. All round logic — initial prioritisation fan-out,
main-phase planning + scoring, per-round view boundary, recurse into
child questions via ``registry.recurse`` — lives here.

Budget accounting is driven by ``on_dispatch_completed``: each local
dispatch sequence reports its unit-cost, and each recurse is counted
against ``cumulative_spent`` when its subscription future resolves. This
keeps subscriber thresholds honest.

The prioritiser is shared across facades targeting the same question.
Scenario B collisions (parent2 transfers budget after parent1's drain)
are handled by the base class: the round-loop task exits on drain, and
the next ``receive_budget`` respawns it with the stacked allocation.
"""

import asyncio
import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING

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
from rumil.llm import build_system_prompt
from rumil.models import (
    AssessDispatchPayload,
    Call,
    CallType,
    Dispatch,
    RecurseClaimDispatchPayload,
    RecurseDispatchPayload,
    Workspace,
)
from rumil.orchestrators.common import (
    ClaimScore,
    PrioritizationResult,
    RecurseSpec,
    SubquestionScore,
    compute_priority_score,
    create_view_for_question,
    score_items_sequentially,
    update_view_for_question,
)
from rumil.prioritisers.dispatch import DispatchRunner
from rumil.prioritisers.prioritiser import Prioritiser
from rumil.prioritisers.subscription import Subscription
from rumil.settings import get_settings
from rumil.tracing.trace_events import (
    CallTypeFruitScoreItem,
    ClaimScoreItem,
    ContextBuiltEvent,
    DispatchesPlannedEvent,
    DispatchExecutedEvent,
    DispatchTraceItem,
    ErrorEvent,
    PhaseSkippedEvent,
    ScoringCompletedEvent,
    SubquestionScoreItem,
)
from rumil.tracing.tracer import CallTrace, set_trace

if TYPE_CHECKING:
    from rumil.database import DB
    from rumil.tracing.broadcast import Broadcaster

log = logging.getLogger(__name__)


class QuestionPrioritiser(DispatchRunner, Prioritiser):
    summarise_before_assess: bool = True

    def __init__(self, question_id: str, kind: str = "question") -> None:
        Prioritiser.__init__(self, question_id, kind=kind)
        self.db = None  # type: ignore[assignment]
        self.broadcaster: Broadcaster | None = None
        self.ingest_hint: str = ""
        self._budget_cap: int | None = None
        self._consumed: int = 0
        self._invocation: int = 0
        self._executed_since_last_plan: bool = False
        self._initial_call: Call | None = None
        self._initial_call_id: str | None = None
        self._parent_call_id: str | None = None
        self._sequence_id: str | None = None
        self._seq_position: int = 0

    def attach(
        self,
        db: "DB",
        broadcaster: "Broadcaster | None" = None,
        *,
        budget_cap: int | None = None,
        parent_call_id: str | None = None,
        ingest_hint: str = "",
    ) -> None:
        """First-parent-wins attach of DB + broadcaster + context.

        Called by the facade (or ``registry.recurse``) before transferring
        budget. Subsequent attaches from other parents are no-ops for the
        already-set fields so trace events stay anchored under the first
        parent that initiated the investigation.
        """
        if self.db is None:
            self.db = db
        if self.broadcaster is None and broadcaster is not None:
            self.broadcaster = broadcaster
        if self._budget_cap is None and budget_cap is not None:
            self._budget_cap = budget_cap
        if self._parent_call_id is None and parent_call_id is not None:
            self._parent_call_id = parent_call_id
        if ingest_hint and not self.ingest_hint:
            self.ingest_hint = ingest_hint

    def _effective_budget(self, global_remaining: int) -> int:
        if self._budget_cap is not None:
            return min(global_remaining, self._budget_cap - self._consumed)
        return global_remaining

    async def _pacing_params(self) -> tuple[int, int]:
        if self._budget_cap is not None:
            return self._budget_cap, self._consumed
        assert self.db is not None
        return await self.db.get_budget()

    async def create_initial_call(self, parent_call_id: str | None = None) -> str:
        """Idempotent eager-create of the initial PRIORITIZATION call.

        Used by parent orchestrators to get a child_call_id for
        ``DispatchExecutedEvent`` before the round-loop has begun.
        Repeated calls return the same id.
        """
        if self._initial_call_id is not None:
            return self._initial_call_id
        assert self.db is not None
        if self._parent_call_id is None and parent_call_id is not None:
            self._parent_call_id = parent_call_id
        budget = self._effective_budget(await self.db.budget_remaining())
        budget = await self._paced_budget(budget)
        p_call = await self.db.create_call(
            CallType.PRIORITIZATION,
            scope_page_id=self.question_id,
            parent_call_id=parent_call_id or self._parent_call_id,
            budget_allocated=budget,
            workspace=Workspace.PRIORITIZATION,
        )
        self._initial_call = p_call
        self._initial_call_id = p_call.id
        return p_call.id

    async def _needs_initial_prioritization(self) -> bool:
        assert self.db is not None
        judgements = await self.db.get_judgements_for_question(self.question_id)
        if judgements:
            return False
        view = await self.db.get_view_for_question(self.question_id)
        return view is None

    async def _cancel_initial_call(self) -> None:
        if self._initial_call is None:
            return
        assert self.db is not None
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
                phase="initial_prioritization",
                reason="Question already has a judgement or view.",
            )
        )
        await mark_call_completed(
            call,
            self.db,
            "Initial prioritization skipped — question already has a judgement or view.",
        )

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

    async def _ensure_sequence(self) -> None:
        if self._sequence_id is not None or self._parent_call_id is None:
            return
        assert self.db is not None
        seq = await self.db.create_call_sequence(
            parent_call_id=self._parent_call_id,
            scope_question_id=self.question_id,
        )
        self._sequence_id = seq.id
        self._seq_position = 0

    async def _run_round(self, round_budget: int) -> None:
        """One prioritisation round.

        Budget bookkeeping: decrements ``self.budget`` by the actual
        dispatch spend at round end (plus any recurse transfers). This
        mirrors the legacy orchestrator's per-dispatch pacing. When a
        round plans no work, the remaining budget is drained so the
        round loop exits — otherwise we'd spin calling a planner that
        keeps returning empty.
        """
        assert self.db is not None
        await self._ensure_sequence()

        remaining = await self.db.budget_remaining()
        effective = self._effective_budget(remaining)
        if effective <= 0:
            await self.on_dispatch_completed(cost=max(self.budget, 1))
            return

        last_call = effective < LAST_CALL_THRESHOLD
        if last_call:
            batch_budget = effective
        else:
            batch_budget = await self._paced_budget(effective)

        spent_before = self._consumed
        result = await self._get_next_batch(
            batch_budget,
            total_remaining=effective,
            last_call=last_call,
        )

        if not result.dispatch_sequences and not result.recurses:
            await self.on_dispatch_completed(cost=max(self.budget, 1))
            return

        local_tasks: list = []
        if result.dispatch_sequences:
            local_tasks.append(
                self._run_sequences(
                    result.dispatch_sequences,
                    self.question_id,
                    result.call_id,
                )
            )

        recurse_futures: list[asyncio.Future] = []
        recurse_costs: list[int] = []
        registry = self.db.prioritiser_registry()
        for recurse in result.recurses:
            factory = (
                QuestionPrioritiser if recurse.kind == "question" else _resolve_claim_prioritiser()
            )
            future = await registry.recurse(
                recurse.target_question_id,
                budget=recurse.budget,
                factory=factory,
                kind=recurse.kind,
                db=self.db,
                broadcaster=self.broadcaster,
                subscriber=self.question_id,
            )
            recurse_futures.append(future)
            recurse_costs.append(recurse.budget)

        gather_tasks = local_tasks + list(recurse_futures)
        results = await asyncio.gather(*gather_tasks, return_exceptions=True)

        for r in results:
            if isinstance(r, Exception):
                log.error("Concurrent dispatch failed: %s", r, exc_info=r)
                if result.call_id:
                    trace = CallTrace(
                        result.call_id,
                        self.db,
                        broadcaster=self.broadcaster,
                    )
                    await trace.record(
                        ErrorEvent(
                            message=(f"Concurrent dispatch failed: {type(r).__name__}: {r}"),
                            phase="dispatch",
                        )
                    )

        self._executed_since_last_plan = True

        if self._invocation > 1 or last_call:
            existing_view = await self.db.get_view_for_question(self.question_id)
            if existing_view:
                await update_view_for_question(
                    self.question_id,
                    self.db,
                    parent_call_id=self._parent_call_id,
                    broadcaster=self.broadcaster,
                    force=True,
                    sequence_id=self._sequence_id,
                    sequence_position=self._seq_position,
                )
            else:
                await create_view_for_question(
                    self.question_id,
                    self.db,
                    parent_call_id=self._parent_call_id,
                    broadcaster=self.broadcaster,
                    force=True,
                    sequence_id=self._sequence_id,
                    sequence_position=self._seq_position,
                )
            if self._sequence_id is not None:
                self._seq_position += 1

        local_spend = self._consumed - spent_before
        recurse_spend = sum(recurse_costs)
        round_spend = max(local_spend + recurse_spend, 1)
        delivered = result.call_id
        await self.on_dispatch_completed(cost=round_spend, delivered_call_id=delivered)

        if last_call:
            self.budget = 0

    async def _get_next_batch(
        self,
        budget: int,
        total_remaining: int | None = None,
        last_call: bool = False,
    ) -> PrioritizationResult:
        if self._invocation == 0:
            self._invocation += 1
            if await self._needs_initial_prioritization():
                return await self._initial_prioritization(
                    budget,
                    total_remaining=total_remaining,
                    last_call=last_call,
                )
            await self._cancel_initial_call()
            self._executed_since_last_plan = True

        if not self._executed_since_last_plan:
            return PrioritizationResult(dispatch_sequences=[])

        self._executed_since_last_plan = False
        self._invocation += 1
        return await self._main_phase_prioritization(
            budget,
            total_remaining=total_remaining,
            last_call=last_call,
        )

    async def _initial_prioritization(
        self,
        budget: int,
        total_remaining: int | None = None,
        last_call: bool = False,
    ) -> PrioritizationResult:
        assert self.db is not None
        initial_prioritization_budget = budget
        log.info(
            "QuestionPrioritiser initial_prioritization: question=%s, budget=%d",
            self.question_id[:8],
            budget,
        )

        context_text, short_id_map = await build_prioritization_context(
            self.db,
            scope_question_id=self.question_id,
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
                scope_page_id=self.question_id,
                parent_call_id=self._parent_call_id,
                budget_allocated=initial_prioritization_budget,
                workspace=Workspace.PRIORITIZATION,
                sequence_id=self._sequence_id,
                sequence_position=self._seq_position if self._sequence_id else None,
            )
            if self._sequence_id is not None:
                self._seq_position += 1
            self._initial_call_id = p_call.id
        trace = CallTrace(p_call.id, self.db, broadcaster=self.broadcaster)
        set_trace(trace)
        await trace.record(ContextBuiltEvent(budget=initial_prioritization_budget))

        budget_line = (
            f"You have a budget of **{initial_prioritization_budget} research calls** "
            "to distribute among the dispatch tools below."
        )
        if last_call:
            budget_line += (
                " **This is your FINAL allocation — there will be no further "
                "research rounds after this. Spend the full budget on the "
                "highest-value work.**"
            )
        elif total_remaining is not None and total_remaining > initial_prioritization_budget:
            budget_line += (
                f" The overall question has **{total_remaining} budget remaining** "
                "across future rounds."
            )
        task = (
            f"{budget_line}\n\n"
            f"Scope question ID: `{self.question_id}`\n\n"
            "Your job is to call the dispatch tools to fan out exploratory research on "
            "this question. All scout dispatches automatically target the scope question. "
            "You MUST call at least one dispatch tool right now — this is "
            "your only turn and you will not get another chance. Distribute your budget "
            "among the scouting dispatch tools, weighting towards types that seem most "
            "useful for this question and skipping types that are clearly irrelevant. "
            "For each scout you intend to dispatch now, you MUST call its tool on the current turn, "
            "in parallel with all others you intend to dispatch at this point. "
            "Do not do anything else — just dispatch."
        )

        result = await run_prioritization_call(
            task,
            context_text,
            p_call,
            self.db,
            short_id_map=short_id_map,
            dispatch_types=list(
                get_available_calls_preset().initial_prioritization_scouts,
            ),
            system_prompt=build_system_prompt(
                "two_phase_initial_prioritization",
                include_citations=False,
            ),
        )

        dispatches = list(result.dispatches)
        if not dispatches:
            log.warning(
                "Initial prioritization produced no dispatches, synthesizing "
                "default scouts for question=%s",
                self.question_id[:8],
            )
            preset = get_available_calls_preset()
            for ct in preset.initial_prioritization_scouts[:initial_prioritization_budget]:
                ddef = DISPATCH_DEFS[ct]
                dispatches.append(
                    Dispatch(
                        call_type=ct,
                        payload=ddef.schema(
                            question_id=self.question_id,
                            reason="fallback — initial prioritization produced no dispatches",
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
            f"Initial prioritization complete. Planned {len(sequences)} concurrent sequences.",
        )

        log.info(
            "QuestionPrioritiser initial_prioritization complete: %d sequences",
            len(sequences),
        )
        return PrioritizationResult(
            dispatch_sequences=sequences,
            call_id=p_call.id,
        )

    async def _main_phase_prioritization(
        self,
        budget: int,
        total_remaining: int | None = None,
        last_call: bool = False,
    ) -> PrioritizationResult:
        assert self.db is not None
        log.info(
            "QuestionPrioritiser main_phase_prioritization: question=%s, budget=%d, last_call=%s",
            self.question_id[:8],
            budget,
            last_call,
        )

        p_call = await self.db.create_call(
            CallType.PRIORITIZATION,
            scope_page_id=self.question_id,
            parent_call_id=self._parent_call_id,
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

        child_questions = await self.db.get_child_questions(self.question_id)
        parent_question = await self.db.get_page(self.question_id)
        if not parent_question:
            raise RuntimeError(
                f"Parent question {self.question_id} not found. "
                "This usually means the question belongs to a different project "
                "than the current DB scope."
            )

        parent_judgements = await self.db.get_judgements_for_question(self.question_id)
        parent_judgement = (
            max(parent_judgements, key=lambda j: j.created_at) if parent_judgements else None
        )

        scoring_tasks: list = []

        consideration_pages = [
            page for page, _link in await self.db.get_considerations_for_question(self.question_id)
        ]

        scoring_tasks.append(
            score_items_sequentially(
                parent_page=parent_question,
                parent_judgement=parent_judgement,
                items=child_questions,
                system_prompt_name="score_subquestions",
                response_model=SubquestionScore,
                call_id=p_call.id,
                db=self.db,
            )
        )
        scoring_tasks.append(
            score_items_sequentially(
                parent_page=parent_question,
                parent_judgement=parent_judgement,
                items=consideration_pages,
                system_prompt_name="score_claim_items",
                response_model=ClaimScore,
                call_id=p_call.id,
                db=self.db,
            )
        )

        scoring_tasks.append(self.db.get_latest_scout_fruit(self.question_id))

        scoring_results = await asyncio.gather(*scoring_tasks)
        subq_scores: list[dict] = scoring_results[0]
        claim_scores: list[dict] = scoring_results[1]
        scout_fruit: dict[str, int | None] = scoring_results[2]

        await trace.record(
            ScoringCompletedEvent(
                subquestion_scores=[SubquestionScoreItem(**s) for s in subq_scores],
                claim_scores=[ClaimScoreItem(**s) for s in claim_scores],
                per_type_fruit=[
                    CallTypeFruitScoreItem(call_type=ct, fruit=f or 0, reasoning="")
                    for ct, f in scout_fruit.items()
                ],
            )
        )

        scores_text = ""
        if subq_scores:
            lines = ["## Subquestion Scores", ""]
            for s in subq_scores:
                priority = compute_priority_score(
                    s["impact_on_question"],
                    s["broader_impact"],
                    s["fruit"],
                )
                lines.append(
                    f"- `{s['question_id']}` — {s['headline']}: "
                    f"impact_on_q={s['impact_on_question']}, "
                    f"broader={s['broader_impact']}, "
                    f"fruit={s['fruit']}, "
                    f"**priority={priority}** "
                    f"({s['reasoning']})"
                )
            lines.append("")
            scores_text = "\n".join(lines)

        if claim_scores:
            lines = ["## Claim Scores (considerations)", ""]
            for s in claim_scores:
                priority = compute_priority_score(
                    s.get("impact_on_question", 0),
                    s.get("broader_impact", 0),
                    s.get("fruit", 0),
                )
                lines.append(
                    f"- `{s.get('page_id', '?')}` — {s.get('headline', '')}: "
                    f"impact_on_q={s.get('impact_on_question', 0)}, "
                    f"broader={s.get('broader_impact', 0)}, "
                    f"fruit={s.get('fruit', 0)}, "
                    f"**priority={priority}** "
                    f"({s.get('reasoning', '')})"
                )
            lines.append("")
            scores_text += "\n".join(lines)

        if scout_fruit:
            fruit_lines = ["## Per-Scout-Type Remaining Fruit (from latest calls)", ""]
            for ct, f in sorted(scout_fruit.items()):
                fruit_lines.append(
                    f"- **{ct}**: {f}/10" if f is not None else f"- **{ct}**: unknown"
                )
            fruit_lines.append("")
            scores_text += "\n".join(fruit_lines)

        context_text, short_id_map = await build_prioritization_context(
            self.db,
            scope_question_id=self.question_id,
        )
        dispatch_budget = budget if last_call else budget - 1
        budget_line = f"You have a budget of **{dispatch_budget} budget units** to allocate."
        if last_call:
            budget_line += (
                " **This is your FINAL allocation — there will be no further "
                "research rounds after this. Spend the full budget on the "
                "highest-value remaining work.**"
            )
        elif total_remaining is not None and total_remaining > dispatch_budget:
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
            f"Scope question ID: `{self.question_id}`\n\n"
            "## Budget accounting\n\n"
            "Multi-round scouts (find_considerations, scout_*) cost between 1 and "
            "max_rounds budget units depending on early stopping. Dispatches "
            "targeting a **subquestion** (not the scope question) will have an "
            "automatic assess appended, adding 1 to the cost. So a scout with "
            "max_rounds=3 targeting a subquestion costs up to 4 budget units. "
            "Web research and assess dispatches cost exactly 1 each. "
            "Recurse costs exactly the budget you assign.\n\n"
            "Plan conservatively: your total worst-case cost across all dispatches "
            f"must not exceed {dispatch_budget}.\n\n"
            f"{scores_text}\n\n"
            "For each call you intend to dispatch now, you MUST call its tool on the current turn, "
            "in parallel with all others you intend to dispatch at this point. "
            f"Each recurse call must have a budget of at least {MIN_TWOPHASE_BUDGET}."
            f"{ingest_hint}"
        )
        if get_settings().force_twophase_recurse:
            task += (
                "\n\nCRITICAL: You MUST dispatch two recurse calls "
                "if you have enough budget to do so."
            )

        extra_defs: list[DispatchDef] = []
        if dispatch_budget >= MIN_TWOPHASE_BUDGET:
            extra_defs.append(RECURSE_DISPATCH_DEF)
            extra_defs.append(RECURSE_CLAIM_DISPATCH_DEF)

        result = await run_prioritization_call(
            task,
            context_text,
            p_call,
            self.db,
            short_id_map=short_id_map,
            dispatch_types=list(
                get_available_calls_preset().main_phase_prioritization_dispatch,
            ),
            extra_dispatch_defs=extra_defs or None,
            system_prompt=build_system_prompt(
                "two_phase_main_phase_prioritization",
                include_citations=False,
            ),
            dispatch_budget=dispatch_budget,
        )

        sequences: list[list[Dispatch]] = []
        recurses: list[RecurseSpec] = []
        for d in result.dispatches:
            if isinstance(d.payload, RecurseClaimDispatchPayload):
                resolved = await self.db.resolve_page_id(d.payload.question_id)
                if not resolved:
                    log.warning(
                        "Recurse claim ID not found: %s",
                        d.payload.question_id[:8],
                    )
                    continue
                recurses.append(
                    RecurseSpec(
                        target_question_id=resolved,
                        budget=d.payload.budget,
                        kind="claim",
                        reason=d.payload.reason,
                    )
                )
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
                recurses.append(
                    RecurseSpec(
                        target_question_id=resolved,
                        budget=d.payload.budget,
                        kind="question",
                        reason=d.payload.reason,
                    )
                )
                log.info(
                    "Queued recursive investigation: question=%s, budget=%d — %s",
                    resolved[:8],
                    d.payload.budget,
                    d.payload.reason,
                )
            elif d.payload.question_id == self.question_id:
                sequences.append([d])
            else:
                assess = Dispatch(
                    call_type=CallType.ASSESS,
                    payload=AssessDispatchPayload(
                        question_id=d.payload.question_id,
                        reason="Auto-assess after main_phase_prioritization dispatch",
                    ),
                )
                sequences.append([d, assess])

        all_dispatches = [d for seq in sequences for d in seq]
        all_trace_items = [
            DispatchTraceItem(
                call_type=d.call_type.value,
                **d.payload.model_dump(exclude_defaults=True),
            )
            for d in all_dispatches
        ]
        for d in result.dispatches:
            if isinstance(d.payload, (RecurseDispatchPayload, RecurseClaimDispatchPayload)):
                all_trace_items.append(
                    DispatchTraceItem(
                        call_type="recurse",
                        **d.payload.model_dump(exclude_defaults=True),
                    )
                )
        await trace.record(DispatchesPlannedEvent(dispatches=all_trace_items))

        recurse_base = len(all_dispatches)
        registry = self.db.prioritiser_registry()
        for ci, recurse in enumerate(recurses):
            factory = (
                QuestionPrioritiser if recurse.kind == "question" else _resolve_claim_prioritiser()
            )
            child_prio, is_new = await registry.get_or_acquire(
                recurse.target_question_id,
                kind=recurse.kind,
                factory=factory,
            )
            if is_new and hasattr(child_prio, "attach"):
                child_prio.attach(  # type: ignore[attr-defined]
                    self.db,
                    self.broadcaster,
                    budget_cap=recurse.budget,
                    parent_call_id=p_call.id,
                )
            if hasattr(child_prio, "create_initial_call"):
                child_call_id = await child_prio.create_initial_call(  # type: ignore[attr-defined]
                    parent_call_id=p_call.id,
                )
            else:
                child_call_id = None
            child_page = await self.db.get_page(recurse.target_question_id)
            await trace.record(
                DispatchExecutedEvent(
                    index=recurse_base + ci,
                    child_call_type="recurse",
                    question_id=recurse.target_question_id,
                    question_headline=child_page.headline if child_page else "",
                    child_call_id=child_call_id,
                )
            )

        await mark_call_completed(
            p_call,
            self.db,
            f"Main phase prioritization complete. Planned {len(sequences)} "
            f"concurrent sequences, {len(recurses)} recursive children.",
        )

        log.info(
            "QuestionPrioritiser main_phase_prioritization complete: %d sequences, %d recurses",
            len(sequences),
            len(recurses),
        )
        return PrioritizationResult(
            dispatch_sequences=sequences,
            call_id=p_call.id,
            recurses=recurses,
        )

    async def _fire_subscription(self, subscription: Subscription) -> None:
        if self._last_delivered_call_id is not None:
            subscription.resolve(self._last_delivered_call_id)
            return
        if self.db is None:
            subscription.resolve(None)
            return
        try:
            existing = await self.db.get_view_for_question(self.question_id)
            if existing:
                call_id = await update_view_for_question(
                    self.question_id,
                    self.db,
                    broadcaster=self.broadcaster,
                    force=True,
                )
            else:
                call_id = await create_view_for_question(
                    self.question_id,
                    self.db,
                    broadcaster=self.broadcaster,
                    force=True,
                )
        except Exception:
            log.exception(
                "QuestionPrioritiser %s failed to produce force-fire deliverable",
                self.question_id[:8],
            )
            call_id = None
        self._last_delivered_call_id = call_id
        subscription.resolve(call_id)


def _resolve_claim_prioritiser() -> type[Prioritiser]:
    from rumil.prioritisers.claim_prioritiser import ClaimPrioritiser

    return ClaimPrioritiser
