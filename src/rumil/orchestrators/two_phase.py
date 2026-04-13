"""
TwoPhaseOrchestrator: two-phase orchestrator for new questions.
"""

import asyncio
import logging
from collections.abc import Sequence

from rumil.available_calls import get_available_calls_preset
from rumil.calls.common import mark_call_completed
from rumil.calls.dispatches import DISPATCH_DEFS, DispatchDef, RECURSE_CLAIM_DISPATCH_DEF, RECURSE_DISPATCH_DEF
from rumil.calls.prioritization import run_prioritization_call
from rumil.constants import LAST_CALL_THRESHOLD, MIN_TWOPHASE_BUDGET
from rumil.context import build_prioritization_context
from rumil.database import DB
from rumil.llm import build_system_prompt
from rumil.models import (
    AssessDispatchPayload,
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
    SubquestionScore,
    assess_question,
    compute_priority_score,
    create_view_for_question,
    score_items_sequentially,
)
from rumil.settings import get_settings
from rumil.tracing.broadcast import Broadcaster
from rumil.tracing.trace_events import (
    CallTypeFruitScoreItem,
    ClaimScoreItem,
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


class TwoPhaseOrchestrator(BaseOrchestrator):
    """Two-phase orchestrator for new questions.

    Phase 1: Fan out with specialized scouts (subquestions, estimates,
    hypotheses, analogies), then assess.
    Phase 2: Score generated subquestions for impact and remaining fruit,
    then dispatch targeted follow-up (scout, web research, or recurse).
    """

    def __init__(
        self, db: DB,
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
        """Eagerly create the phase-1 prioritization call record.

        Sets ``_call_id`` so the parent can reference this child's call
        before ``run()`` begins. ``_phase1`` reuses the pre-created call.
        """
        budget = self._effective_budget(await self.db.budget_remaining())
        budget = await self._paced_budget(budget)
        phase1_budget = budget
        p_call = await self.db.create_call(
            CallType.PRIORITIZATION,
            scope_page_id=question_id,
            parent_call_id=parent_call_id,
            budget_allocated=phase1_budget,
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
                'TwoPhaseOrchestrator requires a budget of at least '
                f'{MIN_TWOPHASE_BUDGET}, got {effective}'
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

                last_call = effective < LAST_CALL_THRESHOLD
                if last_call:
                    round_budget = effective
                else:
                    round_budget = await self._paced_budget(effective)
                result = await self._get_next_batch(
                    root_question_id, round_budget, total_remaining=effective,
                    last_call=last_call,
                )
                if not result.dispatch_sequences and not result.children:
                    break

                tasks: list = []
                if result.dispatch_sequences:
                    tasks.append(self._run_sequences(
                        result.dispatch_sequences, root_question_id,
                        result.call_id,
                    ))
                for child, child_qid in result.children:
                    tasks.append(child.run(child_qid))

                if not tasks:
                    break

                results = await asyncio.gather(*tasks, return_exceptions=True)
                for r in results:
                    if isinstance(r, Exception):
                        log.error('Concurrent dispatch failed: %s', r, exc_info=r)
                        if result.call_id:
                            trace = CallTrace(
                                result.call_id, self.db,
                                broadcaster=self.broadcaster,
                            )
                            await trace.record(ErrorEvent(
                                message=(
                                    "Concurrent dispatch failed: "
                                    f"{type(r).__name__}: {r}"
                                ),
                                phase="dispatch",
                            ))

                if not any(not isinstance(r, Exception) for r in results):
                    break

                self._executed_since_last_plan = True

                if self._invocation > 1 or last_call:
                    await create_view_for_question(
                        root_question_id, self.db,
                        parent_call_id=self._parent_call_id,
                        broadcaster=self.broadcaster, force=True,
                        sequence_id=self._sequence_id,
                        sequence_position=self._seq_position,
                    )
                    if self._sequence_id is not None:
                        self._seq_position += 1

                if last_call:
                    break
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
            sequence, scope_question_id, parent_call_id, base_index,
            position_in_batch=position_in_batch,
        )
        if result:
            self._consumed += len(sequence)
        return result

    async def _is_new_question(self, question_id: str) -> bool:
        """A question is 'new' if it only has parent-pointer or inline-citation links."""
        links = await self.db.get_links_to(question_id)
        return all(
            l.link_type in (LinkType.CHILD_QUESTION, LinkType.RELATED)
            for l in links
        )

    async def _cancel_initial_call(self) -> None:
        """Mark the eagerly-created phase-1 call as complete when phase 1 is skipped."""
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
        await trace.record(PhaseSkippedEvent(
            phase='phase1',
            reason='Question already has research.',
        ))
        await mark_call_completed(
            call, self.db, 'Phase 1 skipped — question already has research.',
        )

    async def _get_next_batch(
        self,
        question_id: str,
        budget: int,
        parent_call_id: str | None = None,
        total_remaining: int | None = None,
        last_call: bool = False,
    ) -> PrioritizationResult:
        if self._invocation == 0:
            self._invocation += 1
            if await self._is_new_question(question_id):
                return await self._phase1(
                    question_id, budget, parent_call_id,
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
            question_id, budget, self._parent_call_id,
            total_remaining=total_remaining,
            last_call=last_call,
        )

    async def _phase1(
        self,
        question_id: str,
        budget: int,
        parent_call_id: str | None,
        total_remaining: int | None = None,
        last_call: bool = False,
    ) -> PrioritizationResult:
        phase1_budget = budget
        log.info(
            'TwoPhaseOrchestrator phase1: question=%s, budget=%d, phase1_budget=%d',
            question_id[:8], budget, phase1_budget,
        )

        context_text, short_id_map = await build_prioritization_context(
            self.db, scope_question_id=question_id,
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
            f'You have a budget of **{phase1_budget} research calls** to distribute '
            'among the dispatch tools below.'
        )
        if last_call:
            budget_line += (
                ' **This is your FINAL allocation — there will be no further '
                'research rounds after this. Spend the full budget on the '
                'highest-value work.**'
            )
        elif total_remaining is not None and total_remaining > phase1_budget:
            budget_line += (
                f' The overall question has **{total_remaining} budget remaining** '
                'across future rounds.'
            )
        task = (
            f'{budget_line}\n\n'
            f'Scope question ID: `{question_id}`\n\n'
            'Your job is to call the dispatch tools to fan out exploratory research on '
            'this question. All scout dispatches automatically target the scope question. '
            'You MUST call at least one dispatch tool right now — this is '
            'your only turn and you will not get another chance. Distribute your budget '
            'among the scouting dispatch tools, weighting towards types that seem most '
            'useful for this question and skipping types that are clearly irrelevant. '
            'Do not do anything else — just dispatch.'
        )

        result = await run_prioritization_call(
            task, context_text, p_call, self.db,
            short_id_map=short_id_map,
            dispatch_types=list(get_available_calls_preset().phase1_scouts),
            system_prompt_override=build_system_prompt('two_phase_p1'),
        )

        dispatches = list(result.dispatches)
        if not dispatches:
            log.warning(
                'Phase 1 produced no dispatches, synthesizing default scouts '
                'for question=%s', question_id[:8],
            )
            for ct in get_available_calls_preset().phase1_scouts[:phase1_budget]:
                ddef = DISPATCH_DEFS[ct]
                dispatches.append(Dispatch(
                    call_type=ct,
                    payload=ddef.schema(
                        question_id=question_id,
                        reason='fallback — phase 1 produced no dispatches',
                    ),
                ))
        sequences: list[list[Dispatch]] = [[d] for d in dispatches]

        await trace.record(DispatchesPlannedEvent(
            dispatches=[
                DispatchTraceItem(
                    call_type=d.call_type.value,
                    **d.payload.model_dump(exclude_defaults=True),
                )
                for d in dispatches
            ],
        ))

        await mark_call_completed(
            p_call, self.db,
            f'Phase 1 complete. Planned {len(sequences)} concurrent sequences.',
        )

        self._call_id = p_call.id

        log.info(
            'TwoPhaseOrchestrator phase1 complete: %d sequences',
            len(sequences),
        )
        return PrioritizationResult(
            dispatch_sequences=sequences,
            call_id=p_call.id,
        )

    async def _phase2(
        self,
        question_id: str,
        budget: int,
        parent_call_id: str | None,
        total_remaining: int | None = None,
        last_call: bool = False,
    ) -> PrioritizationResult:
        log.info(
            'TwoPhaseOrchestrator phase2: question=%s, budget=%d, last_call=%s',
            question_id[:8], budget, last_call,
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
                f'Parent question {question_id} not found. '
                'This usually means the question belongs to a different project '
                'than the current DB scope.'
            )
        parent_headline = parent_question.headline

        parent_judgements = await self.db.get_judgements_for_question(question_id)
        parent_judgement = (
            max(parent_judgements, key=lambda j: j.created_at)
            if parent_judgements else None
        )

        scoring_tasks: list = []

        consideration_pages = [
            page for page, _link
            in await self.db.get_considerations_for_question(question_id)
        ]

        scoring_tasks.append(score_items_sequentially(
            parent_page=parent_question,
            parent_judgement=parent_judgement,
            items=child_questions,
            system_prompt_name='score_subquestions',
            response_model=SubquestionScore,
            call_id=p_call.id,
            db=self.db,
        ))
        scoring_tasks.append(score_items_sequentially(
            parent_page=parent_question,
            parent_judgement=parent_judgement,
            items=consideration_pages,
            system_prompt_name='score_claim_items',
            response_model=ClaimScore,
            call_id=p_call.id,
            db=self.db,
        ))

        scoring_tasks.append(self.db.get_latest_scout_fruit(question_id))

        scoring_results = await asyncio.gather(*scoring_tasks)
        subq_scores: list[dict] = scoring_results[0]
        claim_scores: list[dict] = scoring_results[1]
        scout_fruit: dict[str, int | None] = scoring_results[2]

        await trace.record(ScoringCompletedEvent(
            subquestion_scores=[
                SubquestionScoreItem(**s) for s in subq_scores
            ],
            claim_scores=[
                ClaimScoreItem(**s) for s in claim_scores
            ],
            per_type_fruit=[
                CallTypeFruitScoreItem(call_type=ct, fruit=f or 0, reasoning='')
                for ct, f in scout_fruit.items()
            ],
        ))

        scores_text = ''
        if subq_scores:
            lines = ['## Subquestion Scores', '']
            for s in subq_scores:
                priority = compute_priority_score(
                    s["impact_on_question"], s["broader_impact"], s["fruit"],
                )
                lines.append(
                    f'- `{s["question_id"]}` — {s["headline"]}: '
                    f'impact_on_q={s["impact_on_question"]}, '
                    f'broader={s["broader_impact"]}, '
                    f'fruit={s["fruit"]}, '
                    f'**priority={priority}** '
                    f'({s["reasoning"]})'
                )
            lines.append('')
            scores_text = '\n'.join(lines)

        if claim_scores:
            lines = ['## Claim Scores (considerations)', '']
            for s in claim_scores:
                priority = compute_priority_score(
                    s.get("impact_on_question", 0),
                    s.get("broader_impact", 0),
                    s.get("fruit", 0),
                )
                lines.append(
                    f'- `{s.get("page_id", "?")}` — {s.get("headline", "")}: '
                    f'impact_on_q={s.get("impact_on_question", 0)}, '
                    f'broader={s.get("broader_impact", 0)}, '
                    f'fruit={s.get("fruit", 0)}, '
                    f'**priority={priority}** '
                    f'({s.get("reasoning", "")})'
                )
            lines.append('')
            scores_text += '\n'.join(lines)

        if scout_fruit:
            fruit_lines = ['## Per-Scout-Type Remaining Fruit (from latest calls)', '']
            for ct, f in sorted(scout_fruit.items()):
                fruit_lines.append(
                    f'- **{ct}**: {f}/10' if f is not None
                    else f'- **{ct}**: unknown'
                )
            fruit_lines.append('')
            scores_text += '\n'.join(fruit_lines)

        context_text, short_id_map = await build_prioritization_context(
            self.db, scope_question_id=question_id,
        )
        dispatch_budget = budget if last_call else budget - 1
        budget_line = f'You have a budget of **{dispatch_budget} budget units** to allocate.'
        if last_call:
            budget_line += (
                ' **This is your FINAL allocation — there will be no further '
                'research rounds after this. Spend the full budget on the '
                'highest-value remaining work.**'
            )
        elif total_remaining is not None and total_remaining > dispatch_budget:
            budget_line += (
                f' The overall question has **{total_remaining} budget remaining** '
                'across future rounds.'
            )
        ingest_hint = ''
        if self.ingest_hint:
            ingest_hint = f'\n\n**Note:** {self.ingest_hint}'
            self.ingest_hint = ''

        task = (
            f'{budget_line}\n\n'
            f'Scope question ID: `{question_id}`\n\n'
            '## Budget accounting\n\n'
            'Multi-round scouts (find_considerations, scout_*) cost between 1 and '
            'max_rounds budget units depending on early stopping. Dispatches '
            'targeting a **subquestion** (not the scope question) will have an '
            'automatic assess appended, adding 1 to the cost. So a scout with '
            'max_rounds=3 targeting a subquestion costs up to 4 budget units. '
            'Web research and assess dispatches cost exactly 1 each. '
            'Recurse costs exactly the budget you assign.\n\n'
            'Plan conservatively: your total worst-case cost across all dispatches '
            f'must not exceed {dispatch_budget}.\n\n'
            f'{scores_text}\n\n'
            'You must make all your dispatch calls now — this is your only turn. '
            f'Each recurse call must have a budget of at least {MIN_TWOPHASE_BUDGET}.'
            f'{ingest_hint}'
        )
        if get_settings().force_twophase_recurse:
            task += (
                '\n\nCRITICAL: You MUST dispatch two recurse calls '
                'if you have enough budget to do so.'
            )

        extra_defs: list[DispatchDef] = []
        if dispatch_budget >= MIN_TWOPHASE_BUDGET:
            extra_defs.append(RECURSE_DISPATCH_DEF)
            extra_defs.append(RECURSE_CLAIM_DISPATCH_DEF)

        result = await run_prioritization_call(
            task, context_text, p_call, self.db,
            short_id_map=short_id_map,
            dispatch_types=list(get_available_calls_preset().phase2_dispatch),
            extra_dispatch_defs=extra_defs or None,
            system_prompt_override=build_system_prompt('two_phase_p2'),
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
                        'Recurse claim ID not found: %s',
                        d.payload.question_id[:8],
                    )
                    continue
                child_claim = ClaimInvestigationOrchestrator(
                    self.db, self.broadcaster, budget_cap=d.payload.budget,
                )
                child_claim._parent_call_id = p_call.id
                children.append((child_claim, resolved))
                log.info(
                    'Queued recursive claim investigation: claim=%s, budget=%d — %s',
                    resolved[:8], d.payload.budget, d.payload.reason,
                )
            elif isinstance(d.payload, RecurseDispatchPayload):
                resolved = await self.db.resolve_page_id(d.payload.question_id)
                if not resolved:
                    log.warning(
                        'Recurse question ID not found: %s',
                        d.payload.question_id[:8],
                    )
                    continue
                child = TwoPhaseOrchestrator(
                    self.db, self.broadcaster, budget_cap=d.payload.budget,
                )
                child._parent_call_id = p_call.id
                children.append((child, resolved))
                log.info(
                    'Queued recursive investigation: question=%s, budget=%d — %s',
                    resolved[:8], d.payload.budget, d.payload.reason,
                )
            elif d.payload.question_id == question_id:
                sequences.append([d])
            else:
                assess = Dispatch(
                    call_type=CallType.ASSESS,
                    payload=AssessDispatchPayload(
                        question_id=d.payload.question_id,
                        reason='Auto-assess after phase-2 dispatch',
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
                all_trace_items.append(DispatchTraceItem(
                    call_type='recurse',
                    **d.payload.model_dump(exclude_defaults=True),
                ))
        await trace.record(DispatchesPlannedEvent(dispatches=all_trace_items))

        recurse_base = len(all_dispatches)
        child_pages = await self.db.get_pages_by_ids(
            [child_qid for _, child_qid in children]
        )
        for ci, (child, child_qid) in enumerate(children):
            child_call_id = await child.create_initial_call(
                child_qid, parent_call_id=p_call.id,
            )
            child_page = child_pages.get(child_qid)
            await trace.record(DispatchExecutedEvent(
                index=recurse_base + ci,
                child_call_type='recurse',
                question_id=child_qid,
                question_headline=child_page.headline if child_page else '',
                child_call_id=child_call_id,
            ))

        await mark_call_completed(
            p_call, self.db,
            f'Phase 2 complete. Planned {len(sequences)} concurrent sequences, '
            f'{len(children)} recursive children.',
        )

        self._call_id = p_call.id

        log.info(
            'TwoPhaseOrchestrator phase2 complete: %d sequences, %d children',
            len(sequences), len(children),
        )
        return PrioritizationResult(
            dispatch_sequences=sequences,
            call_id=p_call.id,
            children=children,
        )
