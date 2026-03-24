"""Pluggable prioritization: abstract interface and LLM-based implementation."""

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import BaseModel, Field

from rumil.calls import run_prioritization
from rumil.calls.base import link_orphaned_questions
from rumil.calls.dispatches import DISPATCH_DEFS, RECURSE_DISPATCH_DEF
from rumil.calls.prioritization import run_prioritization_call
from rumil.context import build_prioritization_context
from rumil.database import DB
from rumil.llm import LLMExchangeMetadata, build_system_prompt, build_user_message, structured_call
from rumil.models import (
    AssessDispatchPayload,
    CallType,
    Dispatch,
    LinkType,
    MoveType,
    Page,
    PrioritizationDispatchPayload,
    RecurseDispatchPayload,
    ScoutDispatchPayload,
    ScoutMode,
    Workspace,
)
from rumil.page_graph import PageGraph
from rumil.tracing.broadcast import Broadcaster
from rumil.tracing.trace_events import (
    ContextBuiltEvent,
    DispatchesPlannedEvent,
    DispatchTraceItem,
    ScoringCompletedEvent,
    SubquestionScoreItem,
)
from rumil.tracing.tracer import CallTrace
from rumil.calls.common import mark_call_completed

log = logging.getLogger(__name__)

DEFAULT_FRUIT_THRESHOLD = 4
DEFAULT_MAX_ROUNDS = 5

PRIORITIZATION_MOVES: list[MoveType] = [
    MoveType.CREATE_QUESTION,
    MoveType.LINK_CHILD_QUESTION,
]

PHASE1_SCOUT_TYPES: Sequence[CallType] = [
    CallType.SCOUT_SUBQUESTIONS,
    CallType.SCOUT_ESTIMATES,
    CallType.SCOUT_HYPOTHESES,
    CallType.SCOUT_ANALOGIES,
    CallType.SCOUT_PARADIGM_CASES,
    CallType.SCOUT_FACTCHECKS,
]

PHASE2_DISPATCH_TYPES: Sequence[CallType] = [
    CallType.SCOUT_SUBQUESTIONS,
    CallType.SCOUT_ESTIMATES,
    CallType.SCOUT_HYPOTHESES,
    CallType.SCOUT_ANALOGIES,
    CallType.SCOUT_PARADIGM_CASES,
    CallType.SCOUT_FACTCHECKS,
    CallType.WEB_RESEARCH,
]


class SubquestionScore(BaseModel):
    question_id: str = Field(description='Full UUID of the subquestion')
    headline: str = Field(description='Headline of the subquestion')
    impact: int = Field(description='0-10: how much answering this helps the parent')
    fruit: int = Field(description='0-10: how much useful investigation remains')
    reasoning: str = Field(description='Brief explanation of scores')


class SubquestionScoringResult(BaseModel):
    scores: list[SubquestionScore]


class FruitResult(BaseModel):
    fruit: int = Field(description='0-10: how much useful investigation remains')
    reasoning: str = Field(description='Brief explanation')


async def _count_subtree_questions(question_id: str, graph: PageGraph) -> int:
    """Count all descendant questions (not including the question itself)."""
    children = await graph.get_child_questions(question_id)
    count = len(children)
    for child in children:
        count += await _count_subtree_questions(child.id, graph)
    return count


async def _describe_child_questions(
    children: Sequence[Page], graph: PageGraph,
) -> str:
    """Build enriched descriptions of child questions with research stats."""
    lines = []
    for c in children:
        considerations = await graph.get_considerations_for_question(c.id)
        judgements = await graph.get_judgements_for_question(c.id)
        subtree_count = await _count_subtree_questions(c.id, graph)

        parts = []
        parts.append(f'{len(considerations)} considerations')
        if judgements:
            parts.append(f'{len(judgements)} judgement{"s" if len(judgements) != 1 else ""}')
        if subtree_count:
            parts.append(f'{subtree_count} subquestion{"s" if subtree_count != 1 else ""}')

        stats = ', '.join(parts) if parts else 'no research yet'
        lines.append(f'- `{c.id}` — {c.headline} ({stats})')
    return '\n'.join(lines)


@dataclass
class PrioritizationResult:
    dispatch_sequences: Sequence[Sequence[Dispatch]]
    call_id: str | None = None
    trace: CallTrace | None = None


class Prioritizer(ABC):
    @abstractmethod
    async def get_calls(
        self,
        question_id: str,
        budget: int,
        parent_call_id: str | None = None,
    ) -> PrioritizationResult:
        ...


class LLMPrioritizer(Prioritizer):
    """Cursor-based prioritizer that delegates to the LLM prioritization call.

    Maintains an internal plan (list of dispatches) and a cursor. Each
    ``get_calls()`` invocation returns the next batch of executable
    (find_considerations/assess) dispatches. When a sub-prioritization dispatch is
    encountered, it is expanded inline by running a fresh prioritization
    call scoped to that question.
    """

    def __init__(self, db: DB, broadcaster: Broadcaster | None = None):
        self._db = db
        self._broadcaster = broadcaster
        self._plan: list[Dispatch] = []
        self._cursor: int = 0
        self._call_id: str | None = None
        self._trace: CallTrace | None = None
        self._executed_since_last_plan: bool = False
        self._first_call: bool = True

    async def get_calls(
        self,
        question_id: str,
        budget: int,
        parent_call_id: str | None = None,
    ) -> PrioritizationResult:
        if self._cursor >= len(self._plan):
            if not self._first_call and not self._executed_since_last_plan:
                return PrioritizationResult(dispatch_sequences=[])

            await self._run_prioritization(question_id, budget, parent_call_id)
            self._first_call = False
            self._executed_since_last_plan = False

            if not self._plan:
                return self._synthesize_default(question_id)

        batch: list[Dispatch] = []
        while self._cursor < len(self._plan):
            dispatch = self._plan[self._cursor]

            if isinstance(dispatch.payload, PrioritizationDispatchPayload):
                if batch:
                    break
                await self._expand_sub_prioritization(
                    dispatch, parent_call_id,
                )
                continue

            batch.append(dispatch)
            self._cursor += 1

        return PrioritizationResult(
            dispatch_sequences=[batch] if batch else [],
            call_id=self._call_id,
            trace=self._trace,
        )

    def mark_executed(self) -> None:
        """Signal that at least one dispatch from the last batch was executed."""
        self._executed_since_last_plan = True

    async def _run_prioritization(
        self,
        question_id: str,
        budget: int,
        parent_call_id: str | None,
    ) -> None:
        p_call = await self._db.create_call(
            CallType.PRIORITIZATION,
            scope_page_id=question_id,
            parent_call_id=parent_call_id,
            budget_allocated=budget,
            workspace=Workspace.PRIORITIZATION,
        )

        plan = await run_prioritization(
            scope_question_id=question_id,
            call=p_call,
            budget=budget,
            db=self._db,
            broadcaster=self._broadcaster,
        )

        self._plan = list(plan.get('dispatches', []))
        self._cursor = 0
        self._call_id = p_call.id
        self._trace = plan.get('trace')

        log.debug(
            'LLMPrioritizer: got %d dispatches for question=%s',
            len(self._plan), question_id[:8],
        )

    async def _expand_sub_prioritization(
        self,
        dispatch: Dispatch,
        parent_call_id: str | None,
    ) -> None:
        """Replace a PrioritizationDispatch at the cursor with its expansion."""
        payload = dispatch.payload
        assert isinstance(payload, PrioritizationDispatchPayload)

        resolved = await self._db.resolve_page_id(payload.question_id)
        if not resolved:
            log.warning(
                'Sub-prioritization question ID not found: %s',
                payload.question_id[:8],
            )
            self._cursor += 1
            return

        d_label = await self._db.page_label(resolved)
        log.info(
            'Expanding sub-prioritization on %s (budget=%d) — %s',
            d_label, payload.budget, payload.reason,
        )

        p_call = await self._db.create_call(
            CallType.PRIORITIZATION,
            scope_page_id=resolved,
            parent_call_id=self._call_id or parent_call_id,
            budget_allocated=payload.budget,
            workspace=Workspace.PRIORITIZATION,
        )

        plan = await run_prioritization(
            scope_question_id=resolved,
            call=p_call,
            budget=payload.budget,
            db=self._db,
            broadcaster=self._broadcaster,
        )

        sub_dispatches = list(plan.get('dispatches', []))
        self._plan[self._cursor:self._cursor + 1] = sub_dispatches
        self._call_id = p_call.id
        self._trace = plan.get('trace')

        log.debug(
            'Sub-prioritization expanded to %d dispatches',
            len(sub_dispatches),
        )

    def _synthesize_default(self, question_id: str) -> PrioritizationResult:
        """Return default find_considerations+assess when the LLM produces no dispatches."""
        log.info(
            'No dispatches from prioritization, synthesizing default '
            'find_considerations+assess for question=%s', question_id[:8],
        )
        return PrioritizationResult(
            dispatch_sequences=[[
                Dispatch(
                    call_type=CallType.FIND_CONSIDERATIONS,
                    payload=ScoutDispatchPayload(
                        question_id=question_id,
                        mode=ScoutMode.ALTERNATE,
                        fruit_threshold=DEFAULT_FRUIT_THRESHOLD,
                        max_rounds=DEFAULT_MAX_ROUNDS,
                        reason="fallback"
                    ),
                ),
                Dispatch(
                    call_type=CallType.ASSESS,
                    payload=AssessDispatchPayload(
                        question_id=question_id,
                        reason="fallback"
                    ),
                ),
            ]],
            call_id=self._call_id,
            trace=self._trace,
        )


class TwoPhasePrioritizer(Prioritizer):
    """Two-phase prioritizer for new questions.

    Phase 1: Fan out with specialized scouts (subquestions, estimates,
    hypotheses, analogies), then assess.
    Phase 2: Score generated subquestions for impact and remaining fruit,
    then dispatch targeted follow-up (scout, web research, or recurse).
    """

    def __init__(self, db: DB, broadcaster: Broadcaster | None = None):
        self._db = db
        self._broadcaster = broadcaster
        self._has_planned: bool = False
        self._phase1_complete: bool = False
        self._call_id: str | None = None
        self._trace: CallTrace | None = None
        self._executed_since_last_plan: bool = False
        self._pending_children: list[tuple['TwoPhasePrioritizer', str, int]] = []
        self._active_child: tuple['TwoPhasePrioritizer', str, int, int] | None = None
        self._needs_scope_assess: bool = False

    async def get_calls(
        self,
        question_id: str,
        budget: int,
        parent_call_id: str | None = None,
    ) -> PrioritizationResult:
        log.info(
            'TwoPhasePrioritizer.get_calls: question=%s, budget=%d, '
            'has_planned=%s, executed_since_last=%s, pending_children=%d, '
            'active_child=%s, needs_assess=%s',
            question_id[:8], budget, self._has_planned,
            self._executed_since_last_plan, len(self._pending_children),
            bool(self._active_child), self._needs_scope_assess,
        )

        child_result = await self._service_children(budget, parent_call_id)
        if child_result is not None:
            return child_result

        if self._needs_scope_assess:
            self._needs_scope_assess = False
            self._executed_since_last_plan = True
            return PrioritizationResult(
                dispatch_sequences=[[
                    Dispatch(
                        call_type=CallType.ASSESS,
                        payload=AssessDispatchPayload(
                            question_id=question_id,
                            reason='Assess scope question after phase-2 investigation',
                        ),
                    ),
                ]],
                call_id=self._call_id,
                trace=self._trace,
            )

        if self._has_planned and not self._executed_since_last_plan:
            return PrioritizationResult(dispatch_sequences=[])

        self._has_planned = True
        self._executed_since_last_plan = False

        graph = await PageGraph.load(self._db)
        children = await graph.get_child_questions(question_id)

        all_links_from = graph._links_from.get(question_id, [])
        child_q_links = [
            l for l in all_links_from if l.link_type == LinkType.CHILD_QUESTION
        ]
        log.info(
            'Child question check: question=%s, children_found=%d, '
            'total_links_from_scope=%d, child_question_links=%d, '
            'graph_pages=%d',
            question_id[:8], len(children), len(all_links_from),
            len(child_q_links), len(graph._pages),
        )
        for l in child_q_links:
            target = graph._pages.get(l.to_page_id)
            log.info(
                '  child_question link -> %s: page_found=%s, active=%s',
                l.to_page_id[:8],
                target is not None,
                target.is_active() if target else 'N/A',
            )

        if self._phase1_complete or children:
            log.info(
                'Scope question has %d child questions — running phase 2',
                len(children),
            )
            result = await self._phase2(question_id, budget, parent_call_id)

            if not result.dispatch_sequences and self._pending_children:
                child_result = await self._service_children(budget, parent_call_id)
                if child_result is not None:
                    return child_result

            return result

        log.info('No child questions yet — running phase 1')
        return await self._phase1(question_id, budget, parent_call_id)

    def mark_executed(self) -> None:
        if self._active_child:
            self._active_child[0].mark_executed()
        else:
            self._executed_since_last_plan = True

    async def _service_children(
        self,
        budget: int,
        parent_call_id: str | None,
    ) -> PrioritizationResult | None:
        """Delegate to active/pending child prioritizers.

        Returns a result when a child has dispatches to execute, or None
        when all children are done and the parent should resume.
        """
        had_children = self._active_child is not None or bool(self._pending_children)

        while True:
            if self._active_child:
                child_p, child_qid, child_alloc, start_budget = self._active_child
                spent = start_budget - budget
                child_remaining = max(0, child_alloc - spent)

                if child_remaining <= 0:
                    log.info(
                        'Child budget exhausted: question=%s', child_qid[:8],
                    )
                    self._active_child = None
                    continue

                result = await child_p.get_calls(
                    child_qid, child_remaining, parent_call_id,
                )
                if result.dispatch_sequences:
                    return result

                self._active_child = None
                continue

            if self._pending_children:
                child_p, child_qid, child_alloc = self._pending_children.pop(0)
                log.info(
                    'Activating child prioritizer: question=%s, budget=%d',
                    child_qid[:8], child_alloc,
                )
                self._active_child = (child_p, child_qid, child_alloc, budget)
                continue

            if had_children:
                self._executed_since_last_plan = True
            return None

    async def _phase1(
        self,
        question_id: str,
        budget: int,
        parent_call_id: str | None,
    ) -> PrioritizationResult:
        phase1_budget = budget
        log.info(
            'NewQuestionPrioritizer phase1: question=%s, budget=%d, phase1_budget=%d',
            question_id[:8], budget, phase1_budget,
        )

        graph = await PageGraph.load(self._db)
        context_text, short_id_map = await build_prioritization_context(
            self._db, scope_question_id=question_id, graph=graph,
        )


        p_call = await self._db.create_call(
            CallType.PRIORITIZATION,
            scope_page_id=question_id,
            parent_call_id=parent_call_id,
            budget_allocated=phase1_budget,
            workspace=Workspace.PRIORITIZATION,
        )
        trace = CallTrace(p_call.id, self._db, broadcaster=self._broadcaster)
        await trace.record(ContextBuiltEvent(budget=phase1_budget))

        task = (
            f'You have a budget of **{phase1_budget} research calls** to distribute '
            'among the dispatch tools below.\n\n'
            f'Scope question ID: `{question_id}`\n\n'
            'Your job is to call the dispatch tools to fan out exploratory research on '
            'this question. You MUST call at least one dispatch tool right now — this is '
            'your only turn and you will not get another chance. Distribute your budget '
            'among the scouting dispatch tools, weighting towards types that seem most '
            'useful for this question and skipping types that are clearly irrelevant. '
            'Each dispatch costs 1 budget unit.\n\n'
            'You may optionally create subquestions before dispatching. '
            'Do not do anything else — just dispatch.'
        )

        result = await run_prioritization_call(
            task, context_text, p_call, self._db,
            available_moves=PRIORITIZATION_MOVES,
            short_id_map=short_id_map,
            trace=trace,
            dispatch_types=list(PHASE1_SCOUT_TYPES),
            system_prompt_override=build_system_prompt('two_phase_p1'),
        )

        await link_orphaned_questions(
            result.created_page_ids, question_id, self._db,
        )

        dispatches = list(result.dispatches)
        if not dispatches:
            log.warning(
                'Phase 1 produced no dispatches, synthesizing default scouts '
                'for question=%s', question_id[:8],
            )
            for ct in PHASE1_SCOUT_TYPES[:phase1_budget]:
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
            p_call, self._db,
            f'Phase 1 complete. Planned {len(sequences)} concurrent sequences.',
        )

        self._phase1_complete = True
        self._call_id = p_call.id
        self._trace = trace

        log.info(
            'NewQuestionPrioritizer phase1 complete: %d sequences',
            len(sequences),
        )
        return PrioritizationResult(
            dispatch_sequences=sequences,
            call_id=p_call.id,
            trace=trace,
        )

    async def _phase2(
        self,
        question_id: str,
        budget: int,
        parent_call_id: str | None,
    ) -> PrioritizationResult:
        log.info(
            'NewQuestionPrioritizer phase2: question=%s, budget=%d',
            question_id[:8], budget,
        )

        p_call = await self._db.create_call(
            CallType.PRIORITIZATION,
            scope_page_id=question_id,
            parent_call_id=parent_call_id,
            budget_allocated=budget,
            workspace=Workspace.PRIORITIZATION,
        )
        trace = CallTrace(p_call.id, self._db, broadcaster=self._broadcaster)
        await trace.record(ContextBuiltEvent(budget=budget))

        graph = await PageGraph.load(self._db)
        children = await graph.get_child_questions(question_id)
        parent_question = await graph.get_page(question_id)
        parent_headline = parent_question.headline if parent_question else question_id[:8]

        scoring_system = build_system_prompt('score_subquestions')

        scoring_tasks = []
        if children:
            child_descriptions = await _describe_child_questions(children, graph)
            subq_user_msg = build_user_message(
                f'Parent question: {parent_headline}\n\n'
                f'Subquestions to score:\n{child_descriptions}',
                'Score each subquestion on impact and fruit.',
            )
            scoring_tasks.append(structured_call(
                scoring_system,
                user_message=subq_user_msg,
                response_model=SubquestionScoringResult,
                metadata=LLMExchangeMetadata(
                    call_id=p_call.id,
                    phase='score_subquestions',
                    trace=trace,
                ),
                db=self._db,
            ))
        else:
            async def _empty_scores():
                return type('R', (), {'data': {'scores': []}})()
            scoring_tasks.append(_empty_scores())

        fruit_user_msg = build_user_message(
            f'Question: {parent_headline}\n\n'
            f'Question ID: `{question_id}`',
            'Score the remaining fruit on this question only. '
            'Respond with the fruit score and reasoning.',
        )
        scoring_tasks.append(structured_call(
            scoring_system,
            user_message=fruit_user_msg,
            response_model=FruitResult,
            metadata=LLMExchangeMetadata(
                call_id=p_call.id,
                phase='score_parent_fruit',
                trace=trace,
            ),
            db=self._db,
        ))

        scoring_results = await asyncio.gather(*scoring_tasks)
        subq_result = scoring_results[0]
        fruit_result = scoring_results[1]

        subq_scores = subq_result.data.get('scores', []) if subq_result.data else []
        parent_fruit = fruit_result.data.get('fruit', 5) if fruit_result.data else 5

        await trace.record(ScoringCompletedEvent(
            subquestion_scores=[
                SubquestionScoreItem(**s) for s in subq_scores
            ],
            parent_fruit=parent_fruit,
            parent_fruit_reasoning=(
                fruit_result.data.get('reasoning', '') if fruit_result.data else ''
            ),
        ))

        scores_text = ''
        if subq_scores:
            lines = ['## Subquestion Scores', '']
            for s in subq_scores:
                lines.append(
                    f'- `{s["question_id"]}` — {s["headline"]}: '
                    f'impact={s["impact"]}, fruit={s["fruit"]} '
                    f'({s["reasoning"]})'
                )
            lines.append('')
            scores_text = '\n'.join(lines)

        scores_text += (
            f'\n## Parent Question Fruit\n\n'
            f'Remaining fruit on parent: {parent_fruit}/10\n'
        )

        context_text, short_id_map = await build_prioritization_context(
            self._db, scope_question_id=question_id, graph=graph,
        )


        task = (
            f'You have a budget of **{budget} budget units** to allocate.\n\n'
            f'Scope question ID: `{question_id}`\n\n'
            f'{scores_text}\n\n'
            'Use `recurse_into_subquestion` to investigate high-impact, high-fruit '
            'subquestions listed above. You MUST recurse into at least one '
            'subquestion. Use specialized scout dispatches if the '
            'scope question itself needs more exploration. Use `dispatch_web_research` '
            'only on fact-check questions.\n\n'
            'You must make all your dispatch calls now — this is your only turn.'
        )

        result = await run_prioritization_call(
            task, context_text, p_call, self._db,
            available_moves=[],
            short_id_map=short_id_map,
            trace=trace,
            dispatch_types=list(PHASE2_DISPATCH_TYPES),
            extra_dispatch_defs=[RECURSE_DISPATCH_DEF],
        )

        sequences: list[list[Dispatch]] = []
        for d in result.dispatches:
            if isinstance(d.payload, RecurseDispatchPayload):
                resolved = await self._db.resolve_page_id(d.payload.question_id)
                if not resolved:
                    log.warning(
                        'Recurse question ID not found: %s',
                        d.payload.question_id[:8],
                    )
                    continue
                child = TwoPhasePrioritizer(self._db, self._broadcaster)
                self._pending_children.append(
                    (child, resolved, d.payload.budget),
                )
                log.info(
                    'Queued recursive investigation: question=%s, budget=%d — %s',
                    resolved[:8], d.payload.budget, d.payload.reason,
                )
            else:
                sequences.append([d])

        if sequences or self._pending_children:
            self._needs_scope_assess = True

        all_dispatches = [d for seq in sequences for d in seq]
        await trace.record(DispatchesPlannedEvent(
            dispatches=[
                DispatchTraceItem(
                    call_type=d.call_type.value,
                    **d.payload.model_dump(exclude_defaults=True),
                )
                for d in all_dispatches
            ],
        ))

        await mark_call_completed(
            p_call, self._db,
            f'Phase 2 complete. Planned {len(sequences)} concurrent sequences.',
        )

        self._call_id = p_call.id
        self._trace = trace

        log.info(
            'NewQuestionPrioritizer phase2 complete: %d sequences',
            len(sequences),
        )
        return PrioritizationResult(
            dispatch_sequences=sequences,
            call_id=p_call.id,
            trace=trace,
        )
