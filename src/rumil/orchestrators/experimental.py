"""
ExperimentalOrchestrator: experimental orchestrator for trying new strategies.

Currently an exact copy of TwoPhaseOrchestrator.
"""

import asyncio
import logging
from collections.abc import Sequence
from datetime import UTC, datetime

from rumil.available_calls import get_available_calls_preset
from rumil.calls.common import mark_call_completed
from rumil.calls.dispatches import DISPATCH_DEFS, DispatchDef, RECURSE_DISPATCH_DEF
from rumil.calls.prioritization import run_prioritization_call
from rumil.constants import MIN_TWOPHASE_BUDGET
from rumil.context import build_prioritization_context
from rumil.database import DB
from rumil.llm import build_system_prompt
from rumil.models import (
    AssessDispatchPayload,
    Call,
    CallType,
    Dispatch,
    RecurseDispatchPayload,
    Workspace,
)
from rumil.orchestrators.base import BaseOrchestrator
from rumil.orchestrators.common import (
    PrioritizationResult,
    SubquestionScore,
    assess_question,
    score_items_sequentially,
)
from rumil.calls.link_subquestions import LinkSubquestionsCall
from rumil.settings import get_settings
from rumil.tracing.broadcast import Broadcaster
from rumil.tracing.trace_events import (
    CallTypeFruitScoreItem,
    ContextBuiltEvent,
    DispatchExecutedEvent,
    DispatchesPlannedEvent,
    DispatchTraceItem,
    ErrorEvent,
    PhaseSkippedEvent,
    ScoringCompletedEvent,
    SubquestionScoreItem,
)
from rumil.tracing.tracer import CallTrace, set_trace


log = logging.getLogger(__name__)


class ExperimentalOrchestrator(BaseOrchestrator):
    """Experimental orchestrator for trying new strategies.

    Currently an exact copy of TwoPhaseOrchestrator.

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
        budget_cap: int | None = None,
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
        self._last_linker_eval_at: datetime | None = None

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
        question_id: str,
        parent_call_id: str | None = None,
    ) -> str:
        """Eagerly create the initial_prioritization call record.

        Sets ``_call_id`` so the parent can reference this child's call
        before ``run()`` begins. ``_initial_prioritization`` reuses the
        pre-created call.
        """
        budget = self._effective_budget(await self.db.budget_remaining())
        budget = await self._paced_budget(budget)
        initial_prioritization_budget = budget
        p_call = await self.db.create_call(
            CallType.PRIORITIZATION,
            scope_page_id=question_id,
            parent_call_id=parent_call_id,
            budget_allocated=initial_prioritization_budget,
            workspace=Workspace.PRIORITIZATION,
        )
        self._call_id = p_call.id
        self._initial_call = p_call
        self._parent_call_id = parent_call_id
        return p_call.id

    async def run(self, root_question_id: str) -> None:
        own_db = await self.db.fork()
        self.db = own_db
        await self._setup()
        remaining = await self.db.budget_remaining()
        effective = self._effective_budget(remaining)
        if effective < MIN_TWOPHASE_BUDGET:
            raise ValueError(
                "ExperimentalOrchestrator requires a budget of at least "
                f"{MIN_TWOPHASE_BUDGET}, got {effective}"
            )
        if self._parent_call_id:
            seq = await self.db.create_call_sequence(
                parent_call_id=self._parent_call_id,
                scope_question_id=root_question_id,
            )
            self._sequence_id = seq.id
            self._seq_position = 0
        try:
            while True:
                remaining = await self.db.budget_remaining()
                effective = self._effective_budget(remaining)
                if effective <= 0:
                    break

                round_budget = await self._paced_budget(effective)
                result = await self._get_next_batch(
                    root_question_id,
                    round_budget,
                    total_remaining=effective,
                )
                if not result.dispatch_sequences and not result.children:
                    break

                tasks: list = []
                if result.dispatch_sequences:
                    tasks.append(
                        self._run_sequences(
                            result.dispatch_sequences,
                            root_question_id,
                            result.call_id,
                        )
                    )
                for child, child_qid in result.children:
                    tasks.append(child.run(child_qid))

                if not tasks:
                    break

                results = await asyncio.gather(*tasks, return_exceptions=True)
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
                                    message=(
                                        "Concurrent dispatch failed: "
                                        f"{type(r).__name__}: {r}"
                                    ),
                                    phase="dispatch",
                                )
                            )

                if not any(not isinstance(r, Exception) for r in results):
                    break

                self._executed_since_last_plan = True

                if self._invocation > 1:
                    await assess_question(
                        root_question_id,
                        self.db,
                        parent_call_id=self._parent_call_id,
                        broadcaster=self.broadcaster,
                        force=True,
                        sequence_id=self._sequence_id,
                        sequence_position=self._seq_position,
                    )
                    if self._sequence_id is not None:
                        self._seq_position += 2
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

    async def _needs_initial_prioritization(self, question_id: str) -> bool:
        """Run initial_prioritization iff no judgement answers the question yet."""
        judgements = await self.db.get_judgements_for_question(question_id)
        return not judgements

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
                reason="Question already has a judgement.",
            )
        )
        await mark_call_completed(
            call,
            self.db,
            "Initial prioritization skipped — question already has a judgement.",
        )

    async def _get_next_batch(
        self,
        question_id: str,
        budget: int,
        parent_call_id: str | None = None,
        total_remaining: int | None = None,
    ) -> PrioritizationResult:
        if self._invocation == 0:
            self._invocation += 1
            if await self._needs_initial_prioritization(question_id):
                return await self._initial_prioritization(
                    question_id,
                    budget,
                    parent_call_id,
                    total_remaining=total_remaining,
                )
            await self._cancel_initial_call()
            self._executed_since_last_plan = True

        if not self._executed_since_last_plan:
            return PrioritizationResult(dispatch_sequences=[])

        await self._maybe_rerun_linker(question_id, self._parent_call_id)

        self._executed_since_last_plan = False
        self._invocation += 1
        return await self._main_phase_prioritization(
            question_id,
            budget,
            self._parent_call_id,
            total_remaining=total_remaining,
        )

    async def _run_subquestion_linker(
        self,
        question_id: str,
        parent_call_id: str | None,
    ) -> None:
        """Run the LinkSubquestionsCall and update the linker eval timestamp.

        Any failure is logged and swallowed — the orchestrator must proceed regardless.
        """
        try:
            call = await self.db.create_call(
                CallType.LINK_SUBQUESTIONS,
                scope_page_id=question_id,
                parent_call_id=parent_call_id,
            )
            runner = LinkSubquestionsCall(
                question_id,
                call,
                self.db,
                broadcaster=self.broadcaster,
            )
            await runner.run()
        except Exception as e:
            log.warning(
                "Subquestion linker failed for question=%s: %s",
                question_id[:8],
                e,
                exc_info=True,
            )
        finally:
            self._last_linker_eval_at = datetime.now(UTC)

    async def _maybe_rerun_linker(
        self,
        question_id: str,
        parent_call_id: str | None,
    ) -> None:
        """Re-run the linker if enough pages have been added since the last evaluation."""
        if self._last_linker_eval_at is None:
            return
        settings = get_settings()
        count = await self.db.count_pages_since(self._last_linker_eval_at)
        if count >= settings.linker_cache_invalidation_threshold:
            log.info(
                "Linker cache invalidation: %d pages since last eval, re-running "
                "for question=%s",
                count,
                question_id[:8],
            )
            await self._run_subquestion_linker(question_id, parent_call_id)

    async def _initial_prioritization(
        self,
        question_id: str,
        budget: int,
        parent_call_id: str | None,
        total_remaining: int | None = None,
    ) -> PrioritizationResult:
        initial_prioritization_budget = budget
        log.info(
            "ExperimentalOrchestrator initial_prioritization: question=%s, "
            "budget=%d, initial_prioritization_budget=%d",
            question_id[:8],
            budget,
            initial_prioritization_budget,
        )

        await self._run_subquestion_linker(question_id, parent_call_id)

        context_text, short_id_map = await build_prioritization_context(
            self.db,
            scope_question_id=question_id,
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
            )
            if self._sequence_id is not None:
                self._seq_position += 1
        trace = CallTrace(p_call.id, self.db, broadcaster=self.broadcaster)
        set_trace(trace)
        await trace.record(ContextBuiltEvent(budget=initial_prioritization_budget))

        budget_line = (
            f"You have a budget of **{initial_prioritization_budget} research calls** "
            "to distribute among the dispatch tools below."
        )
        if (
            total_remaining is not None
            and total_remaining > initial_prioritization_budget
        ):
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
            "For each scout you indend to dispatch now, you MUST call its tool on the curret turn, "
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
            system_prompt=build_system_prompt("two_phase_initial_prioritization"),
        )

        dispatches = list(result.dispatches)
        if not dispatches:
            log.warning(
                "Initial prioritization produced no dispatches, synthesizing "
                "default scouts for question=%s",
                question_id[:8],
            )
            preset = get_available_calls_preset()
            for ct in preset.initial_prioritization_scouts[
                :initial_prioritization_budget
            ]:
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
            f"Initial prioritization complete. Planned {len(sequences)} "
            "concurrent sequences.",
        )

        self._call_id = p_call.id

        log.info(
            "ExperimentalOrchestrator initial_prioritization complete: %d sequences",
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
    ) -> PrioritizationResult:
        log.info(
            "ExperimentalOrchestrator main_phase_prioritization: question=%s, budget=%d",
            question_id[:8],
            budget,
        )

        p_call = await self.db.create_call(
            CallType.PRIORITIZATION,
            scope_page_id=question_id,
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
            max(parent_judgements, key=lambda j: j.created_at)
            if parent_judgements
            else None
        )

        scoring_tasks: list = []
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
        scoring_tasks.append(self.db.get_latest_scout_fruit(question_id))

        scoring_results = await asyncio.gather(*scoring_tasks)
        subq_scores: list[dict] = scoring_results[0]
        scout_fruit: dict[str, int | None] = scoring_results[1]

        await trace.record(
            ScoringCompletedEvent(
                subquestion_scores=[SubquestionScoreItem(**s) for s in subq_scores],
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
                lines.append(
                    f"- `{s['question_id']}` — {s['headline']}: "
                    f"impact_on_q={s['impact_on_question']}, "
                    f"broader={s['broader_impact']}, "
                    f"fruit={s['fruit']} "
                    f"({s['reasoning']})"
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

        context_text, short_id_map = await build_prioritization_context(
            self.db,
            scope_question_id=question_id,
        )
        dispatch_budget = budget - 1
        budget_line = (
            f"You have a budget of **{dispatch_budget} budget units** to allocate."
        )
        if total_remaining is not None and total_remaining > dispatch_budget:
            budget_line += (
                f" The overall question has **{total_remaining} budget remaining** "
                "across future rounds."
            )
        task = (
            f"{budget_line}\n\n"
            f"Scope question ID: `{question_id}`\n\n"
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
            "For each call you indend to dispatch now, you MUST call its tool on the curret turn, "
            "in parallel with all others you intend to dispatch at this point. "
            f"Each recurse call must have a budget of at least {MIN_TWOPHASE_BUDGET}."
        )
        if get_settings().force_twophase_recurse:
            task += (
                "\n\nCRITICAL: You MUST dispatch two recurse calls "
                "if you have enough budget to do so."
            )

        extra_defs: list[DispatchDef] = []
        if dispatch_budget >= MIN_TWOPHASE_BUDGET:
            extra_defs.append(RECURSE_DISPATCH_DEF)

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
            ),
            dispatch_budget=dispatch_budget,
        )

        sequences: list[list[Dispatch]] = []
        children: list[tuple[ExperimentalOrchestrator, str]] = []
        for d in result.dispatches:
            if isinstance(d.payload, RecurseDispatchPayload):
                resolved = await self.db.resolve_page_id(d.payload.question_id)
                if not resolved:
                    log.warning(
                        "Recurse question ID not found: %s",
                        d.payload.question_id[:8],
                    )
                    continue
                child = ExperimentalOrchestrator(
                    self.db,
                    self.broadcaster,
                    budget_cap=d.payload.budget,
                )
                child._parent_call_id = p_call.id
                children.append((child, resolved))
                log.info(
                    "Queued recursive investigation: question=%s, budget=%d — %s",
                    resolved[:8],
                    d.payload.budget,
                    d.payload.reason,
                )
            elif d.payload.question_id == question_id:
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
            if isinstance(d.payload, RecurseDispatchPayload):
                all_trace_items.append(
                    DispatchTraceItem(
                        call_type="recurse",
                        **d.payload.model_dump(exclude_defaults=True),
                    )
                )
        await trace.record(DispatchesPlannedEvent(dispatches=all_trace_items))

        recurse_base = len(all_dispatches)
        child_pages = await self.db.get_pages_by_ids(
            [child_qid for _, child_qid in children]
        )
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
            "ExperimentalOrchestrator main_phase_prioritization complete: "
            "%d sequences, %d children",
            len(sequences),
            len(children),
        )
        return PrioritizationResult(
            dispatch_sequences=sequences,
            call_id=p_call.id,
            children=children,
        )
