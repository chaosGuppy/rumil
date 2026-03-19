"""Pluggable prioritization: abstract interface and LLM-based implementation."""

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import BaseModel, Field

from rumil.calls import run_prioritization
from rumil.calls.dispatches import DISPATCH_DEFS, RECURSE_DISPATCH_DEF
from rumil.calls.prioritization import run_prioritization_call
from rumil.context import build_prioritization_context, collect_subtree_ids
from rumil.database import DB
from rumil.llm import build_system_prompt, build_user_message, structured_call
from rumil.models import (
    AssessDispatchPayload,
    CallType,
    Dispatch,
    PrioritizationDispatchPayload,
    RecurseDispatchPayload,
    ScoutDispatchPayload,
    ScoutMode,
    Workspace,
)
from rumil.page_graph import PageGraph
from rumil.tracing.broadcast import Broadcaster
from rumil.tracing.trace_events import ContextBuiltEvent, DispatchesPlannedEvent
from rumil.tracing.tracer import CallTrace

log = logging.getLogger(__name__)

DEFAULT_FRUIT_THRESHOLD = 4
DEFAULT_MAX_ROUNDS = 5

PHASE1_SCOUT_TYPES: Sequence[CallType] = [
    CallType.SCOUT_SUBQUESTIONS,
    CallType.SCOUT_ESTIMATES,
    CallType.SCOUT_HYPOTHESES,
    CallType.SCOUT_ANALOGIES,
]

PHASE2_DISPATCH_TYPES: Sequence[CallType] = [
    CallType.SCOUT,
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


@dataclass
class PrioritizationResult:
    dispatches: Sequence[Dispatch]
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
                return PrioritizationResult(dispatches=[])

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
            dispatches=batch,
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
            dispatches=[
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
            ],
            call_id=self._call_id,
            trace=self._trace,
        )


class NewQuestionPrioritizer(Prioritizer):
    """Two-phase prioritizer for new questions.

    Phase 1: Fan out with specialized scouts (subquestions, estimates,
    hypotheses, analogies), then assess.
    Phase 2: Score generated subquestions for impact and remaining fruit,
    then dispatch targeted follow-up (scout, web research, or recurse).
    """

    def __init__(self, db: DB, broadcaster: Broadcaster | None = None):
        self._db = db
        self._broadcaster = broadcaster
        self._invocation: int = 0
        self._call_id: str | None = None
        self._trace: CallTrace | None = None
        self._executed_since_last_plan: bool = False

    async def get_calls(
        self,
        question_id: str,
        budget: int,
        parent_call_id: str | None = None,
    ) -> PrioritizationResult:
        if self._invocation == 0:
            self._invocation += 1
            return await self._phase1(question_id, budget, parent_call_id)

        if not self._executed_since_last_plan:
            return PrioritizationResult(dispatches=[])

        self._executed_since_last_plan = False
        self._invocation += 1
        return await self._phase2(question_id, budget, parent_call_id)

    def mark_executed(self) -> None:
        self._executed_since_last_plan = True

    async def _phase1(
        self,
        question_id: str,
        budget: int,
        parent_call_id: str | None,
    ) -> PrioritizationResult:
        phase1_budget = min(budget - 1, 3)
        log.info(
            'NewQuestionPrioritizer phase1: question=%s, budget=%d, phase1_budget=%d',
            question_id[:8], budget, phase1_budget,
        )

        graph = await PageGraph.load(self._db)
        context_text, short_id_map = await build_prioritization_context(
            self._db, scope_question_id=question_id, graph=graph,
        )
        subtree_ids = await collect_subtree_ids(question_id, self._db, graph=graph)

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
            f'You have a budget of **{phase1_budget} research calls** to allocate.\n\n'
            f'Scope question ID: `{question_id}`\n\n'
            'This is a new question. Use specialized scouts to build initial structure: '
            'subquestions to decompose it, estimates for quantitative angles, '
            'hypotheses for competing explanations, and analogies for illuminating '
            'parallels. You may skip types that are not useful for this question. '
            'Each dispatch costs 1 budget unit.'
        )

        result = await run_prioritization_call(
            task, context_text, p_call, self._db,
            subtree_ids=subtree_ids,
            short_id_map=short_id_map,
            trace=trace,
            dispatch_types=list(PHASE1_SCOUT_TYPES),
        )

        dispatches = list(result.dispatches)
        dispatches.append(Dispatch(
            call_type=CallType.ASSESS,
            payload=AssessDispatchPayload(
                question_id=question_id,
                reason='Post-phase-1 assessment',
            ),
        ))

        await trace.record(DispatchesPlannedEvent(
            dispatches=[
                {
                    'call_type': d.call_type.value,
                    **d.payload.model_dump(exclude_defaults=True),
                }
                for d in dispatches
            ],
        ))

        from rumil.calls.common import complete_call
        await complete_call(
            p_call, self._db,
            f'Phase 1 complete. Planned {len(dispatches)} dispatches.',
        )

        self._call_id = p_call.id
        self._trace = trace

        log.info(
            'NewQuestionPrioritizer phase1 complete: %d dispatches',
            len(dispatches),
        )
        return PrioritizationResult(
            dispatches=dispatches,
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

        children = await self._db.get_child_questions(question_id)
        parent_question = await self._db.get_page(question_id)
        parent_headline = parent_question.headline if parent_question else question_id[:8]

        scoring_system = build_system_prompt('score_subquestions')

        scoring_tasks = []
        if children:
            child_descriptions = '\n'.join(
                f'- `{c.id}` — {c.headline}'
                for c in children
            )
            subq_user_msg = build_user_message(
                f'Parent question: {parent_headline}\n\n'
                f'Subquestions to score:\n{child_descriptions}',
                'Score each subquestion on impact and fruit.',
            )
            scoring_tasks.append(structured_call(
                scoring_system,
                user_message=subq_user_msg,
                response_model=SubquestionScoringResult,
                max_tokens=2048,
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
            max_tokens=512,
        ))

        scoring_results = await asyncio.gather(*scoring_tasks)
        subq_result = scoring_results[0]
        fruit_result = scoring_results[1]

        subq_scores = subq_result.data.get('scores', []) if subq_result.data else []
        parent_fruit = fruit_result.data.get('fruit', 5) if fruit_result.data else 5

        scores_text = ''
        if subq_scores:
            lines = ['## Subquestion Scores', '']
            for s in subq_scores:
                lines.append(
                    f'- `{s["question_id"][:8]}` — {s["headline"]}: '
                    f'impact={s["impact"]}, fruit={s["fruit"]} '
                    f'({s["reasoning"]})'
                )
            lines.append('')
            scores_text = '\n'.join(lines)

        scores_text += (
            f'\n## Parent Question Fruit\n\n'
            f'Remaining fruit on parent: {parent_fruit}/10\n'
        )

        graph = await PageGraph.load(self._db)
        context_text, short_id_map = await build_prioritization_context(
            self._db, scope_question_id=question_id, graph=graph,
        )
        subtree_ids = await collect_subtree_ids(question_id, self._db, graph=graph)

        p_call = await self._db.create_call(
            CallType.PRIORITIZATION,
            scope_page_id=question_id,
            parent_call_id=parent_call_id,
            budget_allocated=budget,
            workspace=Workspace.PRIORITIZATION,
        )
        trace = CallTrace(p_call.id, self._db, broadcaster=self._broadcaster)
        await trace.record(ContextBuiltEvent(budget=budget))

        task = (
            f'You have a budget of **{budget} research calls** to allocate.\n\n'
            f'Scope question ID: `{question_id}`\n\n'
            'Phase 1 (specialized scouts + assess) is complete. Now plan '
            'targeted follow-up based on what was discovered.\n\n'
            f'{scores_text}\n\n'
            'Dispatch further investigation: use dispatch_scout for general '
            'exploration, dispatch_web_research for web-based evidence, or '
            'recurse_into_subquestion to recursively investigate a child '
            'question with its own prioritization cycle. '
            'You can target the parent question or any child question.'
        )

        result = await run_prioritization_call(
            task, context_text, p_call, self._db,
            subtree_ids=subtree_ids,
            short_id_map=short_id_map,
            trace=trace,
            dispatch_types=list(PHASE2_DISPATCH_TYPES),
            extra_dispatch_defs=[RECURSE_DISPATCH_DEF],
        )

        dispatches = list(result.dispatches)

        auto_assess_questions: set[str] = set()
        for d in dispatches:
            if not isinstance(d.payload, RecurseDispatchPayload):
                qid = d.payload.question_id
                if qid not in auto_assess_questions:
                    auto_assess_questions.add(qid)
        for qid in auto_assess_questions:
            dispatches.append(Dispatch(
                call_type=CallType.ASSESS,
                payload=AssessDispatchPayload(
                    question_id=qid,
                    reason='Auto-assess after phase-2 dispatch',
                ),
            ))

        await trace.record(DispatchesPlannedEvent(
            dispatches=[
                {
                    'call_type': d.call_type.value,
                    **d.payload.model_dump(exclude_defaults=True),
                }
                for d in dispatches
            ],
        ))

        from rumil.calls.common import complete_call
        await complete_call(
            p_call, self._db,
            f'Phase 2 complete. Planned {len(dispatches)} dispatches.',
        )

        self._call_id = p_call.id
        self._trace = trace

        log.info(
            'NewQuestionPrioritizer phase2 complete: %d dispatches',
            len(dispatches),
        )
        return PrioritizationResult(
            dispatches=dispatches,
            call_id=p_call.id,
            trace=trace,
        )
