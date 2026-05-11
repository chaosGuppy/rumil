"""
TwoPhaseOrchestrator: two-phase orchestrator for new questions.
"""

import asyncio
import logging

from rumil.available_calls import get_available_calls_preset
from rumil.calls.common import embed_task_for_page, mark_call_completed
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
    RecurseClaimDispatchPayload,
    RecurseDispatchPayload,
    Workspace,
)
from rumil.orchestrators.base import BaseOrchestrator
from rumil.orchestrators.common import (
    ClaimScore,
    PrioritizationResult,
    SubquestionScore,
    compute_priority_score,
    red_team_question,
    score_items_sequentially,
)
from rumil.settings import get_settings
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
    SubquestionScoreItem,
)
from rumil.tracing.tracer import CallTrace, set_trace
from rumil.views import get_active_view

log = logging.getLogger(__name__)


# When the workspace has at least this many active claims at credence ≥6 with
# no source citation, the main-phase prioritizer gets a hint to consider
# `web_research` / `dispatch_web_factcheck` instead of more `scout_*` work.
UNSOURCED_HIGH_CREDENCE_THRESHOLD = 5


class TwoPhaseOrchestrator(BaseOrchestrator):
    """Two-phase orchestrator for new questions.

    Initial prioritization: Fan out with specialized scouts (subquestions,
    estimates, hypotheses, analogies), then assess.
    Main phase prioritization: Score generated subquestions for impact and
    remaining fruit, then dispatch targeted follow-up (scout, web research,
    or recurse).
    """

    def __init__(
        self,
        db: DB,
        broadcaster: Broadcaster | None = None,
        assigned_budget: int | None = None,
        pool_pre_registered: bool = False,
    ):
        super().__init__(db, broadcaster)
        self._invocation: int = 0
        self._call_id: str | None = None

        self._executed_since_last_plan: bool = False
        # The budget assigned to this orchestrator at construction. NOT a
        # runtime cap — it's the amount this orchestrator contributes to its
        # question pool on run() start (when not pool_pre_registered) and the
        # ``allocated`` figure surfaced in RecurseFailedEvent. The pool's
        # ``remaining`` is the authoritative per-question gate during the run
        # loop, so this orchestrator can spend more than ``_assigned_budget``
        # if peer cycles or prior contributions have left surplus in the pool.
        self._assigned_budget: int | None = assigned_budget
        self._initial_call: Call | None = None
        self._parent_call_id: str | None = None
        self._sequence_id: str | None = None
        self._seq_position: int = 0
        # When True, this orchestrator's contribution to the question pool
        # was already registered atomically by the parent's qbp_recurse call,
        # so run() must NOT register again (it would double-count). The
        # finally block still calls qbp_unregister to balance active_calls
        # (which qbp_recurse incremented via its register half).
        self._pool_pre_registered: bool = pool_pre_registered

    def _effective_budget(self, global_remaining: int) -> int:
        return global_remaining

    async def _pacing_params(self) -> tuple[int, int]:
        if self.pool_question_id:
            pool = await self.db.qbp_get(self.pool_question_id)
            if pool.registered:
                return pool.contributed, pool.consumed
        return await self.db.get_budget()

    async def create_initial_call(
        self,
        question_id: str,
        parent_call_id: str | None = None,
    ) -> str:
        """Eagerly create the initial_prioritization call record.

        Sets ``_call_id`` so the parent can reference this child's call
        before ``run()`` begins. ``_initial_prioritization`` reuses the
        pre-created call.
        """
        budget = (
            self._assigned_budget
            if self._assigned_budget is not None
            else await self.db.budget_remaining()
        )
        budget = await self._paced_budget(budget)
        initial_prioritization_budget = budget
        p_call = await self.db.create_call(
            CallType.PRIORITIZATION,
            scope_page_id=question_id,
            parent_call_id=parent_call_id,
            budget_allocated=initial_prioritization_budget,
            workspace=Workspace.PRIORITIZATION,
            call_params={"phase": "initial"},
        )
        self._call_id = p_call.id
        self._initial_call = p_call
        self._parent_call_id = parent_call_id
        return p_call.id

    async def run(self, root_question_id: str) -> None:
        own_db = await self.db.fork()
        self.db = own_db
        await self._setup()
        if self._pool_pre_registered:
            pool = await self.db.qbp_get(root_question_id)
            effective = max(pool.remaining, 0)
        else:
            remaining = await self.db.budget_remaining()
            effective = self._effective_budget(remaining)
        if effective < MIN_TWOPHASE_BUDGET:
            raise ValueError(
                "TwoPhaseOrchestrator requires a budget of at least "
                f"{MIN_TWOPHASE_BUDGET}, got {effective}"
            )
        if self._parent_call_id:
            seq = await self.db.create_call_sequence(
                parent_call_id=self._parent_call_id,
                scope_question_id=root_question_id,
            )
            self._sequence_id = seq.id
            self._seq_position = 0
        self.pool_question_id = root_question_id
        if not self._pool_pre_registered:
            contribution = self._assigned_budget if self._assigned_budget is not None else effective
            await self.db.qbp_register(root_question_id, contribution)
        try:
            while True:
                remaining = await self.db.budget_remaining()
                pool = await self.db.qbp_get(root_question_id)
                effective = min(self._effective_budget(remaining), pool.remaining)
                if effective <= 0:
                    break

                last_call = effective < LAST_CALL_THRESHOLD
                if last_call:
                    round_budget = effective
                else:
                    round_budget = await self._paced_budget(effective)
                result = await self.get_dispatches(
                    root_question_id,
                    round_budget,
                    total_remaining=effective,
                    last_call=last_call,
                )
                if not result.dispatch_sequences and not result.children:
                    break

                results = await self.execute_dispatches(result, root_question_id)
                if not results:
                    break
                if not any(not isinstance(r, Exception) for r in results):
                    break

                self._executed_since_last_plan = True

                if self._invocation > 1 or last_call:
                    view = get_active_view()
                    await view.refresh(
                        root_question_id,
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

                    if get_settings().enable_red_team:
                        await red_team_question(
                            root_question_id,
                            self.db,
                            parent_call_id=self._parent_call_id,
                            broadcaster=self.broadcaster,
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
                await self.db.qbp_unregister(root_question_id)
            finally:
                await self._teardown()
                await own_db.close()

    async def _needs_initial_prioritization(self, question_id: str) -> bool:
        """Run initial_prioritization iff no view answers the question yet."""
        view = get_active_view()
        return not await view.exists(question_id, self.db)

    async def _cancel_initial_call(self) -> None:
        """Mark the eagerly-created initial_prioritization call as complete when it is skipped."""
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
                phase="initial_prioritization",
                reason="Question already has a judgement or view.",
            )
        )
        await mark_call_completed(
            call,
            self.db,
            "Initial prioritization skipped — question already has a judgement or view.",
        )

    async def get_dispatches(
        self,
        root_question_id: str,
        budget: int,
        *,
        parent_call_id: str | None = None,
        total_remaining: int | None = None,
        last_call: bool = False,
    ) -> PrioritizationResult:
        question_id = root_question_id
        if self._invocation == 0:
            self._invocation += 1
            if await self._needs_initial_prioritization(question_id):
                return await self._initial_prioritization(
                    question_id,
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
        return await self._main_phase_prioritization(
            question_id,
            budget,
            self._parent_call_id,
            total_remaining=total_remaining,
            last_call=last_call,
        )

    async def _initial_prioritization(
        self,
        question_id: str,
        budget: int,
        parent_call_id: str | None,
        total_remaining: int | None = None,
        last_call: bool = False,
    ) -> PrioritizationResult:
        initial_prioritization_budget = budget
        log.info(
            "TwoPhaseOrchestrator initial_prioritization: question=%s, budget=%d, "
            "initial_prioritization_budget=%d",
            question_id[:8],
            budget,
            initial_prioritization_budget,
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
                scope_page_id=question_id,
                parent_call_id=parent_call_id,
                budget_allocated=initial_prioritization_budget,
                workspace=Workspace.PRIORITIZATION,
                sequence_id=self._sequence_id,
                sequence_position=self._seq_position if self._sequence_id else None,
                call_params={"phase": "initial"},
            )
            if self._sequence_id is not None:
                self._seq_position += 1

        view = get_active_view()
        await view.refresh(
            question_id,
            self.db,
            parent_call_id=p_call.id,
            broadcaster=self.broadcaster,
            force=True,
            pool_question_id=self.pool_question_id,
        )

        context_text, short_id_map = await build_prioritization_context(
            self.db,
            scope_question_id=question_id,
            current_call_id=p_call.id,
        )
        trace = CallTrace(p_call.id, self.db, broadcaster=self.broadcaster)
        set_trace(trace)
        await trace.record(ContextBuiltEvent(budget=initial_prioritization_budget))

        dispatch_budget = max(initial_prioritization_budget - 1, 1)
        budget_line = (
            f"You have a budget of **{dispatch_budget} research calls** "
            "to distribute among the dispatch tools below."
        )
        if last_call:
            budget_line += (
                " **This is your FINAL allocation — there will be no further "
                "research rounds after this. Spend the full budget on the "
                "highest-value work.**"
            )
        elif total_remaining is not None and total_remaining > dispatch_budget:
            budget_line += (
                f" The overall question has **{total_remaining} budget remaining** "
                "across future rounds."
            )
        task = (
            f"{budget_line}\n\n"
            f"Scope question ID: `{question_id}`\n\n"
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

        scouts = list(get_available_calls_preset().initial_prioritization_scouts)
        if CallType.SCOUT_FACTCHECKS in scouts and not await self.db.has_any_active_claim():
            scouts = [s for s in scouts if s != CallType.SCOUT_FACTCHECKS]
            log.info(
                "Empty workspace — dropping scout_factchecks from initial fan-out "
                "for q=%s (nothing to verify yet).",
                question_id[:8],
            )

        embed_task = await embed_task_for_page(
            self.db, question_id, "fan-out scouting prioritization."
        )
        result = await run_prioritization_call(
            task,
            context_text,
            p_call,
            self.db,
            short_id_map=short_id_map,
            dispatch_types=scouts,
            system_prompt=build_system_prompt(
                "two_phase_initial_prioritization",
                task=embed_task,
                include_citations=False,
                include_per_call=False,
            ),
            prompt_name="two_phase_initial_prioritization",
        )

        dispatches = list(result.dispatches)
        if not dispatches:
            log.warning(
                "Initial prioritization produced no dispatches, synthesizing "
                "default scouts for question=%s",
                question_id[:8],
            )
            for ct in scouts[:dispatch_budget]:
                ddef = DISPATCH_DEFS[ct]
                dispatches.append(
                    Dispatch(
                        call_type=ct,
                        payload=ddef.schema(
                            question_id=question_id,
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

        self._call_id = p_call.id

        log.info(
            "TwoPhaseOrchestrator initial_prioritization complete: %d sequences",
            len(sequences),
        )
        return PrioritizationResult(
            dispatch_sequences=sequences,
            call_id=p_call.id,
        )

    async def _main_phase_prioritization(
        self,
        question_id: str,
        budget: int,
        parent_call_id: str | None,
        total_remaining: int | None = None,
        last_call: bool = False,
    ) -> PrioritizationResult:
        log.info(
            "TwoPhaseOrchestrator main_phase_prioritization: question=%s, budget=%d, last_call=%s",
            question_id[:8],
            budget,
            last_call,
        )

        p_call = await self.db.create_call(
            CallType.PRIORITIZATION,
            scope_page_id=question_id,
            parent_call_id=parent_call_id,
            budget_allocated=budget,
            workspace=Workspace.PRIORITIZATION,
            sequence_id=self._sequence_id,
            sequence_position=self._seq_position if self._sequence_id else None,
            call_params={"phase": "main_phase"},
        )
        if self._sequence_id is not None:
            self._seq_position += 1
        trace = CallTrace(p_call.id, self.db, broadcaster=self.broadcaster)
        set_trace(trace)
        await trace.record(ContextBuiltEvent(budget=budget))

        child_questions = await self.db.get_child_questions(question_id)
        parent_question = await self.db.get_page(question_id)
        if not parent_question:
            raise RuntimeError(
                f"Parent question {question_id} not found. "
                "This usually means the question belongs to a different project "
                "than the current DB scope."
            )

        parent_judgements = await self.db.get_judgements_for_question(question_id)
        parent_judgement = (
            max(parent_judgements, key=lambda j: j.created_at) if parent_judgements else None
        )

        scoring_tasks: list = []

        consideration_pages = [
            page for page, _link in await self.db.get_considerations_for_question(question_id)
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

        scoring_tasks.append(self.db.get_latest_scout_fruit(question_id))

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

        # Take the authoritative pool snapshot here — between this point and
        # the LLM dispatching, peer cycles can only consume more from the pool.
        # Using a fresh snapshot ensures the budget line and the Coordination
        # context section agree on what's available.
        # Skip the bailout when the pool was never registered (e.g. callers
        # that invoke get_dispatches outside the run() loop, like
        # scripts/run_prio.py): "no pool" must not look like "drained to zero".
        fresh_pool = await self.db.qbp_get(question_id)
        if fresh_pool.registered:
            budget = min(budget, max(fresh_pool.remaining, 0))
            # If the pool drained between top-of-loop and now (i.e. peer cycles
            # consumed our slice), bail before calling the LLM with budget 0/-1.
            # The outer loop will break on the empty result.
            if budget <= 0:
                await mark_call_completed(
                    p_call,
                    self.db,
                    "Pool drained by peer cycles before this round could plan.",
                )
                return PrioritizationResult(dispatch_sequences=[], call_id=p_call.id)
        context_text, short_id_map = await build_prioritization_context(
            self.db,
            scope_question_id=question_id,
            current_call_id=p_call.id,
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

        unsourced_count = await self.db.count_unsourced_high_credence_claims()
        unsourced_hint = ""
        if unsourced_count >= UNSOURCED_HIGH_CREDENCE_THRESHOLD:
            unsourced_hint = (
                f"\n\n**Note:** the workspace currently has **{unsourced_count} active "
                "claims with credence ≥6 that cite no source page** — by default these "
                "are unverified retrievals from training data (typically produced by "
                "`scout_*` calls). Consider whether `dispatch_web_factcheck` against "
                "high-priority `scout_web_questions` outputs, or `web_research` on "
                "load-bearing numeric claims, is in fact higher-EV than further "
                "scouting."
            )

        task = (
            f"{budget_line}\n\n"
            f"Scope question ID: `{question_id}`\n\n"
            "## Budget accounting\n\n"
            "Multi-round scouts (find_considerations, scout_*) cost between 1 and "
            "max_rounds budget units depending on early stopping. Dispatches "
            "targeting a **subquestion** (not the scope question) will have an "
            "automatic view refresh appended, adding 1 to the cost. So a scout "
            "with max_rounds=3 targeting a subquestion costs up to 4 budget units. "
            "Web research and assess dispatches cost exactly 1 each. "
            "Recurse costs exactly the budget you assign.\n\n"
            "Plan conservatively: your total worst-case cost across all dispatches "
            f"must not exceed {dispatch_budget}.\n\n"
            f"{scores_text}\n\n"
            "For each call you intend to dispatch now, you MUST call its tool on the current turn, "
            "in parallel with all others you intend to dispatch at this point. "
            f"Each recurse call must have a budget of at least {MIN_TWOPHASE_BUDGET}."
            f"{ingest_hint}"
            f"{unsourced_hint}"
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

        embed_task = await embed_task_for_page(
            self.db,
            question_id,
            "main-phase prioritization across open lines of research.",
        )
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
                task=embed_task,
                include_citations=False,
                include_per_call=False,
            ),
            prompt_name="two_phase_main_phase_prioritization",
            dispatch_budget=dispatch_budget,
        )

        from rumil.orchestrators.claim_investigation import ClaimInvestigationOrchestrator

        sequences: list[list[Dispatch]] = []
        children: list[tuple[TwoPhaseOrchestrator | ClaimInvestigationOrchestrator, str]] = []
        for d in result.dispatches:
            if isinstance(d.payload, RecurseClaimDispatchPayload):
                resolved = await self.db.resolve_page_id(d.payload.question_id)
                if not resolved:
                    log.warning(
                        "Recurse claim ID not found: %s",
                        d.payload.question_id[:8],
                    )
                    continue
                await self.db.qbp_recurse(question_id, resolved, d.payload.budget)
                child_claim = ClaimInvestigationOrchestrator(
                    self.db,
                    self.broadcaster,
                    assigned_budget=d.payload.budget,
                    pool_pre_registered=True,
                )
                child_claim._parent_call_id = p_call.id
                children.append((child_claim, resolved))
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
                await self.db.qbp_recurse(question_id, resolved, d.payload.budget)
                child = TwoPhaseOrchestrator(
                    self.db,
                    self.broadcaster,
                    assigned_budget=d.payload.budget,
                    pool_pre_registered=True,
                )
                child._parent_call_id = p_call.id
                children.append((child, resolved))
                log.info(
                    "Queued recursive investigation: question=%s, budget=%d — %s",
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
            if isinstance(d.payload, (RecurseDispatchPayload, RecurseClaimDispatchPayload)):
                all_trace_items.append(
                    DispatchTraceItem(
                        call_type="recurse",
                        **d.payload.model_dump(exclude_defaults=True),
                    )
                )
        await trace.record(DispatchesPlannedEvent(dispatches=all_trace_items))

        recurse_base = len(all_dispatches)
        child_pages = await self.db.get_pages_by_ids([child_qid for _, child_qid in children])
        for ci, (child, child_qid) in enumerate(children):
            child_call_id = await child.create_initial_call(
                child_qid,
                parent_call_id=p_call.id,
            )
            child_page = child_pages.get(child_qid)
            await trace.record(
                DispatchExecutedEvent(
                    index=recurse_base + ci,
                    child_call_type="recurse",
                    question_id=child_qid,
                    question_headline=child_page.headline if child_page else "",
                    child_call_id=child_call_id,
                )
            )

        await mark_call_completed(
            p_call,
            self.db,
            f"Main phase prioritization complete. Planned {len(sequences)} "
            f"concurrent sequences, {len(children)} recursive children.",
        )

        self._call_id = p_call.id

        log.info(
            "TwoPhaseOrchestrator main_phase_prioritization complete: %d sequences, %d children",
            len(sequences),
            len(children),
        )
        return PrioritizationResult(
            dispatch_sequences=sequences,
            call_id=p_call.id,
            children=children,
        )
