"""
Orchestrator: drives the research workflow using the prioritization system.
Budget is tracked here; prioritization and review calls are free.
"""

import asyncio
import logging
import os
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import BaseModel, Field

from rumil.calls import run_prioritization
from rumil.calls.common import mark_call_completed
from rumil.calls.dispatches import DISPATCH_DEFS, RECURSE_DISPATCH_DEF
from rumil.calls.prioritization import run_prioritization_call
from rumil.calls.summarize import summarize_question
from rumil.calls.call_registry import (
    ASSESS_CALL_CLASSES,
    ASSESS_CONCEPT_CALL_CLASSES,
    INGEST_CALL_CLASSES,
    FIND_CONSIDERATIONS_CALL_CLASSES,
    SCOUT_CONCEPTS_CALL_CLASSES,
    SCOUT_ANALOGIES_CALL_CLASSES,
    SCOUT_PARADIGM_CASES_CALL_CLASSES,
    SCOUT_ESTIMATES_CALL_CLASSES,
    SCOUT_HYPOTHESES_CALL_CLASSES,
    SCOUT_SUBQUESTIONS_CALL_CLASSES,
    WEB_RESEARCH_CALL_CLASSES,
)
from rumil.context import build_prioritization_context, collect_subtree_ids
from rumil.database import DB
from rumil.embeddings import embed_and_store_page
from rumil.llm import LLMExchangeMetadata, build_system_prompt, build_user_message, structured_call
from rumil.models import (
    AssessDispatchPayload,
    CallType,
    Dispatch,
    MoveType,
    Page,
    PageLayer,
    PageType,
    PrioritizationDispatchPayload,
    RecurseDispatchPayload,
    ScoutAnalogiesDispatchPayload,
    ScoutDispatchPayload,
    ScoutMode,
    ScoutParadigmCasesDispatchPayload,
    ScoutEstimatesDispatchPayload,
    ScoutHypothesesDispatchPayload,
    ScoutSubquestionsDispatchPayload,
    WebResearchDispatchPayload,
    Workspace,
)
from rumil.page_graph import PageGraph
from rumil.settings import get_settings
from rumil.tracing.broadcast import Broadcaster
from rumil.tracing.tracer import CallTrace
from rumil.tracing.trace_events import (
    ContextBuiltEvent,
    DispatchExecutedEvent,
    DispatchesPlannedEvent,
    DispatchTraceItem,
    ScoringCompletedEvent,
    SubquestionScoreItem,
)


log = logging.getLogger(__name__)

DEFAULT_FRUIT_THRESHOLD = 4
DEFAULT_MAX_ROUNDS = 5
DEFAULT_INGEST_FRUIT_THRESHOLD = 5
DEFAULT_INGEST_MAX_ROUNDS = 5

SMOKE_TEST_MAX_ROUNDS = 1
SMOKE_TEST_INGEST_MAX_ROUNDS = 1

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
]

PHASE2_DISPATCH_TYPES: Sequence[CallType] = [
    CallType.FIND_CONSIDERATIONS,
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
    dispatch_sequences: Sequence[Sequence[Dispatch]]
    call_id: str | None = None
    trace: CallTrace | None = None
    children: Sequence[tuple['TwoPhaseOrchestrator', str]] = ()


async def create_root_question(question_text: str, db: DB) -> str:
    page = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=question_text,
        headline=question_text[:120],
        epistemic_status=2.5,
        epistemic_type="open question",
        provenance_model="human",
        provenance_call_type="init",
        provenance_call_id="init",
        extra={"status": "open"},
    )
    await db.save_page(page)
    try:
        await embed_and_store_page(db, page, field_name="abstract")
    except Exception:
        log.warning("Failed to create embedding for root question %s", page.id[:8], exc_info=True)
    return page.id


async def _consume_budget(db: DB, force: bool = False) -> bool:
    """Consume one unit of global budget. Returns False if exhausted.

    When *force* is True the call always succeeds: if normal consumption
    fails, budget is temporarily expanded so the dispatch can proceed.
    This is used to guarantee that every dispatch in a committed batch
    runs, even if it means slightly exceeding the original budget.
    """
    ok = await db.consume_budget(1)
    if not ok:
        if force:
            await db.add_budget(1)
            ok = await db.consume_budget(1)
        if not ok:
            remaining = await db.budget_remaining()
            log.info("Budget exhausted (remaining: %d)", remaining)
    return ok


async def find_considerations_until_done(
    question_id: str,
    db: DB,
    max_rounds: int | None = None,
    fruit_threshold: int = DEFAULT_FRUIT_THRESHOLD,
    parent_call_id: str | None = None,
    context_page_ids: list | None = None,
    mode: ScoutMode = ScoutMode.ALTERNATE,
    broadcaster=None,
    force: bool = False,
) -> tuple[int, list[str]]:
    """Run a cache-aware find-considerations session.

    Creates one Call and delegates to the ScoutCall class, which handles
    multi-round looping with conversation resumption, lightweight fruit
    checks, and a single closing review at the end.

    Returns (rounds_made, list_of_call_ids).
    """
    if max_rounds is None:
        max_rounds = (
            SMOKE_TEST_MAX_ROUNDS if get_settings().is_smoke_test
            else DEFAULT_MAX_ROUNDS
        )
    log.info(
        "find_considerations_until_done: question=%s, max_rounds=%d, fruit_threshold=%d, mode=%s",
        question_id[:8], max_rounds, fruit_threshold, mode.value,
    )

    if force and await db.budget_remaining() <= 0:
        await db.add_budget(1)

    call = await db.create_call(
        CallType.FIND_CONSIDERATIONS,
        scope_page_id=question_id,
        parent_call_id=parent_call_id,
        context_page_ids=context_page_ids,
    )

    cls = FIND_CONSIDERATIONS_CALL_CLASSES[get_settings().find_considerations_call_variant]
    scout = cls(
        question_id, call, db,
        max_rounds=max_rounds,
        fruit_threshold=fruit_threshold,
        mode=mode,
        context_page_ids=context_page_ids,
        broadcaster=broadcaster,
    )
    await scout.run()

    log.info(
        "find_considerations_until_done finished: %d rounds, call=%s",
        scout.rounds_completed, call.id[:8],
    )
    return scout.rounds_completed, [call.id]


async def ingest_until_done(
    source_page: Page,
    question_id: str,
    db: DB,
    max_rounds: int | None = None,
    fruit_threshold: int = DEFAULT_INGEST_FRUIT_THRESHOLD,
    parent_call_id: str | None = None,
    broadcaster=None,
) -> int:
    """
    Run Ingest rounds on a source/question pair until remaining_fruit falls below
    fruit_threshold or max_rounds is reached. Returns number of Ingest calls made.
    fruit_threshold is the primary stopping condition; max_rounds is a failsafe.
    Each round sees previously extracted claims via the question's working context.
    """
    if max_rounds is None:
        max_rounds = (
            SMOKE_TEST_INGEST_MAX_ROUNDS if get_settings().is_smoke_test
            else DEFAULT_INGEST_MAX_ROUNDS
        )
    log.info(
        "ingest_until_done: source=%s, question=%s, max_rounds=%d",
        source_page.id[:8], question_id[:8], max_rounds,
    )
    rounds = 0
    for i in range(max_rounds):
        if not await _consume_budget(db):
            break

        call = await db.create_call(
            CallType.INGEST,
            scope_page_id=source_page.id,
            parent_call_id=parent_call_id,
        )
        cls = INGEST_CALL_CLASSES[get_settings().ingest_call_variant]
        ingest = cls(source_page, question_id, call, db, broadcaster=broadcaster)
        await ingest.run()
        review = ingest.review
        rounds += 1

        remaining_fruit = review.get("remaining_fruit", 5) if review else 5
        log.info(
            "Ingest round %d/%d: remaining_fruit=%d (threshold=%d)",
            i + 1, max_rounds, remaining_fruit, fruit_threshold,
        )

        if remaining_fruit <= fruit_threshold:
            log.info(
                "Ingest fruit (%d) below threshold (%d), stopping",
                remaining_fruit, fruit_threshold,
            )
            break

    log.info("ingest_until_done finished: %d rounds", rounds)
    return rounds


async def assess_question(
    question_id: str,
    db: DB,
    parent_call_id: str | None = None,
    context_page_ids: list | None = None,
    broadcaster=None,
    force: bool = False,
) -> str | None:
    """Run one Assess call on a question. Returns call ID, or None if no budget."""
    log.info("assess_question: question=%s", question_id[:8])
    if not await _consume_budget(db, force=force):
        return None

    await summarize_question(question_id, db, parent_call_id=parent_call_id)

    call = await db.create_call(
        CallType.ASSESS,
        scope_page_id=question_id,
        parent_call_id=parent_call_id,
        context_page_ids=context_page_ids,
    )
    cls = ASSESS_CALL_CLASSES[get_settings().assess_call_variant]
    assess = cls(question_id, call, db, broadcaster=broadcaster)
    await assess.run()
    return call.id


async def _run_assess_concept_loop(
    concept_id: str,
    db: DB,
    phase: str,
    max_rounds: int,
    fruit_threshold: int,
    parent_call_id: str | None = None,
    broadcaster=None,
) -> dict:
    """Run assess_concept rounds until fruit drops below threshold or max_rounds reached.

    Returns the review dict from the final round.
    """
    from rumil.calls.assess_concept import SCREENING_PHASE, VALIDATION_PHASE
    log.info(
        "_run_assess_concept_loop: concept=%s, phase=%s, max_rounds=%d, threshold=%d",
        concept_id[:8], phase, max_rounds, fruit_threshold,
    )
    last_review: dict = {}
    for i in range(max_rounds):
        call = await db.create_call(
            CallType.ASSESS_CONCEPT,
            scope_page_id=concept_id,
            parent_call_id=parent_call_id,
        )
        cls = ASSESS_CONCEPT_CALL_CLASSES[get_settings().assess_call_variant]
        assess = cls(concept_id, call, db, phase=phase, broadcaster=broadcaster)
        await assess.run()
        last_review = assess.concept_assessment

        remaining_fruit = last_review.get("remaining_fruit", 10)
        log.info(
            "Assess concept round %d/%d (%s): fruit=%d, score=%s",
            i + 1, max_rounds, phase,
            remaining_fruit, last_review.get("score"),
        )
        if remaining_fruit <= fruit_threshold:
            log.info(
                "Concept fruit (%d) at or below threshold (%d), stopping %s phase",
                remaining_fruit, fruit_threshold, phase,
            )
            break

    return last_review


async def run_concept_session(
    question_id: str,
    db: DB,
    broadcaster=None,
) -> None:
    """Run a full concept-generation session for a research question.

    1. Scout concepts — generate proposals for the question's subtree.
    2. For each proposal: run stage-1 screening.
    3. If screening passes: automatically run stage-2 validation.
    """
    from rumil.calls.assess_concept import (
        SCREENING_PHASE,
        SCREENING_FRUIT_THRESHOLD,
        SCREENING_MAX_ROUNDS,
        VALIDATION_PHASE,
        VALIDATION_FRUIT_THRESHOLD,
        VALIDATION_MAX_ROUNDS,
    )
    log.info("run_concept_session: question=%s", question_id[:8])

    scout_call = await db.create_call(
        CallType.SCOUT_CONCEPTS,
        scope_page_id=question_id,
    )
    cls = SCOUT_CONCEPTS_CALL_CLASSES["default"]
    scout = cls(question_id, scout_call, db, broadcaster=broadcaster)
    await scout.run()
    proposed_ids = scout.result.created_page_ids

    log.info(
        "Scout concepts complete: %d proposals for question=%s",
        len(proposed_ids), question_id[:8],
    )

    for concept_id in proposed_ids:
        concept = await db.get_page(concept_id)
        label = concept.headline[:60] if concept else concept_id[:8]
        log.info("Screening concept: %s [%s]", label, concept_id[:8])

        screening_review = await _run_assess_concept_loop(
            concept_id, db,
            phase=SCREENING_PHASE,
            max_rounds=SCREENING_MAX_ROUNDS,
            fruit_threshold=SCREENING_FRUIT_THRESHOLD,
            parent_call_id=scout_call.id,
            broadcaster=broadcaster,
        )

        if not screening_review.get("screening_passed"):
            log.info(
                "Concept [%s] did not pass screening (score=%s)",
                concept_id[:8], screening_review.get("score"),
            )
            continue

        log.info(
            "Concept [%s] passed screening (score=%s), proceeding to validation",
            concept_id[:8], screening_review.get("score"),
        )

        validation_review = await _run_assess_concept_loop(
            concept_id, db,
            phase=VALIDATION_PHASE,
            max_rounds=VALIDATION_MAX_ROUNDS,
            fruit_threshold=VALIDATION_FRUIT_THRESHOLD,
            parent_call_id=scout_call.id,
            broadcaster=broadcaster,
        )

        concept_page = await db.get_page(concept_id)
        if concept_page and concept_page.is_superseded:
            log.info("Concept [%s] was promoted to research workspace", concept_id[:8])
        else:
            log.info(
                "Concept [%s] completed validation but was not promoted (score=%s)",
                concept_id[:8], validation_review.get("score"),
            )


async def web_research_question(
    question_id: str,
    db: DB,
    allowed_domains: list[str] | None = None,
    parent_call_id: str | None = None,
    broadcaster=None,
    force: bool = False,
) -> str | None:
    """Run one web research call on a question. Returns call ID, or None if no budget."""
    log.info('web_research_question: question=%s', question_id[:8])
    if not await _consume_budget(db, force=force):
        return None

    call = await db.create_call(
        CallType.WEB_RESEARCH,
        scope_page_id=question_id,
        parent_call_id=parent_call_id,
    )
    cls = WEB_RESEARCH_CALL_CLASSES[get_settings().web_research_call_variant]
    web_research = cls(
        question_id, call, db,
        allowed_domains=allowed_domains,
        broadcaster=broadcaster,
    )
    await web_research.run()
    return call.id


def _create_broadcaster(db: DB) -> Broadcaster | None:
    """Create a broadcaster for the given DB's run_id, or None if disabled."""
    if os.environ.get("RUMIL_TEST_MODE"):
        return None
    settings = get_settings()
    url, key = settings.get_supabase_credentials(prod=settings.is_prod_db)
    return Broadcaster(db.run_id, url, key)


class BaseOrchestrator(ABC):
    def __init__(self, db: DB, broadcaster: Broadcaster | None = None):
        self.db = db
        self.broadcaster: Broadcaster | None = broadcaster
        self._owns_broadcaster: bool = False

    async def _setup(self) -> None:
        if not self.broadcaster:
            self.broadcaster = _create_broadcaster(self.db)
            self._owns_broadcaster = True
        log.info('Orchestrator: run_id=%s', self.db.run_id)
        total, used = await self.db.get_budget()
        log.info(
            'Orchestrator.run starting: budget=%d (used=%d)',
            total, used,
        )

    async def _teardown(self) -> None:
        if self.broadcaster and self._owns_broadcaster:
            await self.broadcaster.close()
        total, used = await self.db.get_budget()
        log.info('Orchestrator.run complete: budget used %d/%d', used, total)

    async def _run_simple_call_dispatch(
        self,
        question_id: str,
        call_type: CallType,
        registry: dict,
        parent_call_id: str | None,
        force: bool = False,
    ) -> str | None:
        """Run a simple (single-pass) call dispatch. Consumes 1 budget."""
        if not await _consume_budget(self.db, force=force):
            return None

        call = await self.db.create_call(
            call_type,
            scope_page_id=question_id,
            parent_call_id=parent_call_id,
        )
        cls = registry['default']
        instance = cls(question_id, call, self.db, broadcaster=self.broadcaster)
        await instance.run()
        return call.id

    async def _run_dispatch_sequence(
        self,
        sequence: Sequence[Dispatch],
        scope_question_id: str,
        parent_call_id: str | None,
        trace: CallTrace | None,
        base_index: int,
    ) -> bool:
        """Run dispatches in a sequence sequentially. Returns True if any executed.

        All dispatches in the sequence are guaranteed to run: if budget
        is exhausted mid-sequence, subsequent dispatches force-consume
        so that trailing calls (e.g. auto-assess) are never skipped.
        """
        executed = False
        for i, dispatch in enumerate(sequence):
            force = i > 0 and await self.db.budget_remaining() <= 0
            resolved, child_call_id = await self._execute_dispatch(
                dispatch, scope_question_id, parent_call_id, force=force,
            )
            executed = True
            if trace:
                await trace.record(DispatchExecutedEvent(
                    index=base_index + i,
                    child_call_type=dispatch.call_type.value,
                    question_id=resolved,
                    child_call_id=child_call_id,
                ))
        return executed

    async def _execute_dispatch(
        self,
        dispatch: Dispatch,
        scope_question_id: str,
        parent_call_id: str | None,
        *,
        force: bool = False,
    ) -> tuple[str, str | None]:
        """Execute a single dispatch.

        When *force* is True, budget is expanded if needed so the call
        always proceeds (used for trailing dispatches in a committed batch).

        Returns (resolved_question_id, child_call_id).
        """
        p = dispatch.payload

        resolved = await self.db.resolve_page_id(p.question_id)
        if not resolved:
            log.warning(
                'Dispatch question ID not found: %s, falling back to scope',
                p.question_id[:8],
            )
            resolved = scope_question_id

        d_label = await self.db.page_label(resolved)
        child_call_id: str | None = None

        if isinstance(p, ScoutDispatchPayload):
            log.info(
                'Dispatch: find_considerations on %s (mode=%s, fruit_threshold=%d, max_rounds=%d) — %s',
                d_label, p.mode.value, p.fruit_threshold, p.max_rounds, p.reason,
            )
            _, child_ids = await find_considerations_until_done(
                resolved,
                self.db,
                max_rounds=p.max_rounds,
                fruit_threshold=p.fruit_threshold,
                parent_call_id=parent_call_id,
                context_page_ids=p.context_page_ids,
                mode=p.mode,
                broadcaster=self.broadcaster,
                force=force,
            )
            child_call_id = child_ids[0] if child_ids else None

        elif isinstance(p, AssessDispatchPayload):
            log.info('Dispatch: assess on %s — %s', d_label, p.reason)
            child_call_id = await assess_question(
                resolved,
                self.db,
                parent_call_id=parent_call_id,
                context_page_ids=p.context_page_ids,
                broadcaster=self.broadcaster,
                force=force,
            )

        elif isinstance(p, ScoutSubquestionsDispatchPayload):
            log.info('Dispatch: scout_subquestions on %s — %s', d_label, p.reason)
            child_call_id = await self._run_simple_call_dispatch(
                resolved, CallType.SCOUT_SUBQUESTIONS,
                SCOUT_SUBQUESTIONS_CALL_CLASSES, parent_call_id,
                force=force,
            )

        elif isinstance(p, ScoutEstimatesDispatchPayload):
            log.info('Dispatch: scout_estimates on %s — %s', d_label, p.reason)
            child_call_id = await self._run_simple_call_dispatch(
                resolved, CallType.SCOUT_ESTIMATES,
                SCOUT_ESTIMATES_CALL_CLASSES, parent_call_id,
                force=force,
            )

        elif isinstance(p, ScoutHypothesesDispatchPayload):
            log.info('Dispatch: scout_hypotheses on %s — %s', d_label, p.reason)
            child_call_id = await self._run_simple_call_dispatch(
                resolved, CallType.SCOUT_HYPOTHESES,
                SCOUT_HYPOTHESES_CALL_CLASSES, parent_call_id,
                force=force,
            )

        elif isinstance(p, ScoutAnalogiesDispatchPayload):
            log.info('Dispatch: scout_analogies on %s — %s', d_label, p.reason)
            child_call_id = await self._run_simple_call_dispatch(
                resolved, CallType.SCOUT_ANALOGIES,
                SCOUT_ANALOGIES_CALL_CLASSES, parent_call_id,
                force=force,
            )

        elif isinstance(p, ScoutParadigmCasesDispatchPayload):
            log.info('Dispatch: scout_paradigm_cases on %s — %s', d_label, p.reason)
            child_call_id = await self._run_simple_call_dispatch(
                resolved, CallType.SCOUT_PARADIGM_CASES,
                SCOUT_PARADIGM_CASES_CALL_CLASSES, parent_call_id,
                force=force,
            )

        elif isinstance(p, WebResearchDispatchPayload):
            log.info('Dispatch: web_research on %s — %s', d_label, p.reason)
            child_call_id = await web_research_question(
                resolved, self.db,
                parent_call_id=parent_call_id,
                broadcaster=self.broadcaster,
                force=force,
            )

        return resolved, child_call_id

    async def _run_sequences(
        self,
        sequences: Sequence[Sequence[Dispatch]],
        scope_question_id: str,
        call_id: str | None,
        trace: CallTrace | None,
    ) -> bool:
        """Run multiple dispatch sequences concurrently. Returns True if any executed."""
        base_index = 0
        tasks = []
        for seq in sequences:
            tasks.append(self._run_dispatch_sequence(
                seq, scope_question_id, call_id,
                trace, base_index,
            ))
            base_index += len(seq)

        sequence_results = await asyncio.gather(*tasks)
        return any(sequence_results)

    @abstractmethod
    async def run(self, root_question_id: str) -> None: ...


class LLMOrchestrator(BaseOrchestrator):
    """Cursor-based orchestrator that delegates planning to the LLM prioritization call.

    Maintains an internal plan (list of dispatches) and a cursor. Each
    loop iteration returns the next batch of executable dispatches. When
    a sub-prioritization dispatch is encountered, it is expanded inline
    by running a fresh prioritization call scoped to that question.
    """

    def __init__(self, db: DB, broadcaster: Broadcaster | None = None):
        super().__init__(db, broadcaster)
        self._plan: list[Dispatch] = []
        self._cursor: int = 0
        self._call_id: str | None = None
        self._trace: CallTrace | None = None
        self._executed_since_last_plan: bool = False
        self._first_call: bool = True

    async def run(self, root_question_id: str) -> None:
        await self._setup()
        try:
            while True:
                remaining = await self.db.budget_remaining()
                if remaining <= 0:
                    break

                result = await self._get_next_batch(root_question_id, remaining)
                if not result.dispatch_sequences:
                    break

                executed = await self._run_sequences(
                    result.dispatch_sequences, root_question_id,
                    result.call_id, result.trace,
                )
                if executed:
                    self._executed_since_last_plan = True
                else:
                    break
        finally:
            await self._teardown()

    async def _get_next_batch(
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

    async def _run_prioritization(
        self,
        question_id: str,
        budget: int,
        parent_call_id: str | None,
    ) -> None:
        p_call = await self.db.create_call(
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
            db=self.db,
            broadcaster=self.broadcaster,
        )

        self._plan = list(plan.get('dispatches', []))
        self._cursor = 0
        self._call_id = p_call.id
        self._trace = plan.get('trace')

        log.debug(
            'LLMOrchestrator: got %d dispatches for question=%s',
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

        resolved = await self.db.resolve_page_id(payload.question_id)
        if not resolved:
            log.warning(
                'Sub-prioritization question ID not found: %s',
                payload.question_id[:8],
            )
            self._cursor += 1
            return

        d_label = await self.db.page_label(resolved)
        log.info(
            'Expanding sub-prioritization on %s (budget=%d) — %s',
            d_label, payload.budget, payload.reason,
        )

        p_call = await self.db.create_call(
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
            db=self.db,
            broadcaster=self.broadcaster,
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
        self._trace: CallTrace | None = None
        self._executed_since_last_plan: bool = False
        self._budget_cap: int | None = budget_cap
        self._consumed: int = 0

    def _effective_budget(self, global_remaining: int) -> int:
        if self._budget_cap is not None:
            return min(global_remaining, self._budget_cap - self._consumed)
        return global_remaining

    async def run(self, root_question_id: str) -> None:
        await self._setup()
        try:
            while True:
                remaining = await self.db.budget_remaining()
                effective = self._effective_budget(remaining)
                if effective <= 0:
                    break

                result = await self._get_next_batch(root_question_id, effective)
                if not result.dispatch_sequences and not result.children:
                    break

                tasks: list = []
                if result.dispatch_sequences:
                    tasks.append(self._run_sequences(
                        result.dispatch_sequences, root_question_id,
                        result.call_id, result.trace,
                    ))
                for child, child_qid in result.children:
                    tasks.append(child.run(child_qid))

                if not tasks:
                    break

                results = await asyncio.gather(*tasks, return_exceptions=True)
                for r in results:
                    if isinstance(r, Exception):
                        log.error('Concurrent dispatch failed: %s', r, exc_info=r)

                if any(not isinstance(r, Exception) for r in results):
                    self._executed_since_last_plan = True
                else:
                    break
        finally:
            await self._teardown()

    async def _run_dispatch_sequence(
        self,
        sequence: Sequence[Dispatch],
        scope_question_id: str,
        parent_call_id: str | None,
        trace: CallTrace | None,
        base_index: int,
    ) -> bool:
        result = await super()._run_dispatch_sequence(
            sequence, scope_question_id, parent_call_id, trace, base_index,
        )
        if result:
            self._consumed += len(sequence)
        return result

    async def _get_next_batch(
        self,
        question_id: str,
        budget: int,
        parent_call_id: str | None = None,
    ) -> PrioritizationResult:
        if self._invocation == 0:
            self._invocation += 1
            return await self._phase1(question_id, budget, parent_call_id)

        if not self._executed_since_last_plan:
            return PrioritizationResult(dispatch_sequences=[])

        self._executed_since_last_plan = False
        self._invocation += 1
        return await self._phase2(question_id, budget, parent_call_id)

    async def _phase1(
        self,
        question_id: str,
        budget: int,
        parent_call_id: str | None,
    ) -> PrioritizationResult:
        phase1_budget = min(budget - 1, 4)
        log.info(
            'TwoPhaseOrchestrator phase1: question=%s, budget=%d, phase1_budget=%d',
            question_id[:8], budget, phase1_budget,
        )

        graph = await PageGraph.load(self.db)
        context_text, short_id_map = await build_prioritization_context(
            self.db, scope_question_id=question_id, graph=graph,
        )
        subtree_ids = await collect_subtree_ids(question_id, self.db, graph=graph)

        p_call = await self.db.create_call(
            CallType.PRIORITIZATION,
            scope_page_id=question_id,
            parent_call_id=parent_call_id,
            budget_allocated=phase1_budget,
            workspace=Workspace.PRIORITIZATION,
        )
        trace = CallTrace(p_call.id, self.db, broadcaster=self.broadcaster)
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
            task, context_text, p_call, self.db,
            available_moves=PRIORITIZATION_MOVES,
            subtree_ids=subtree_ids,
            short_id_map=short_id_map,
            trace=trace,
            dispatch_types=list(PHASE1_SCOUT_TYPES),
            system_prompt_override=build_system_prompt('two_phase_p1'),
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
            p_call, self.db,
            f'Phase 1 complete. Planned {len(sequences)} concurrent sequences.',
        )

        self._call_id = p_call.id
        self._trace = trace

        log.info(
            'TwoPhaseOrchestrator phase1 complete: %d sequences',
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
            'TwoPhaseOrchestrator phase2: question=%s, budget=%d',
            question_id[:8], budget,
        )

        p_call = await self.db.create_call(
            CallType.PRIORITIZATION,
            scope_page_id=question_id,
            parent_call_id=parent_call_id,
            budget_allocated=budget,
            workspace=Workspace.PRIORITIZATION,
        )
        trace = CallTrace(p_call.id, self.db, broadcaster=self.broadcaster)
        await trace.record(ContextBuiltEvent(budget=budget))

        child_questions = await self.db.get_child_questions(question_id)
        parent_question = await self.db.get_page(question_id)
        parent_headline = parent_question.headline if parent_question else question_id[:8]

        scoring_system = build_system_prompt('score_subquestions')

        scoring_tasks = []
        if child_questions:
            child_descriptions = '\n'.join(
                f'- `{c.id}` — {c.headline}'
                for c in child_questions
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
                metadata=LLMExchangeMetadata(
                    call_id=p_call.id,
                    phase='score_subquestions',
                    trace=trace,
                ),
                db=self.db,
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
            db=self.db,
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

        graph = await PageGraph.load(self.db)
        context_text, short_id_map = await build_prioritization_context(
            self.db, scope_question_id=question_id, graph=graph,
        )
        subtree_ids = await collect_subtree_ids(question_id, self.db, graph=graph)

        task = (
            f'You have a budget of **{budget} research calls** to allocate during this rollout.\n\n'
            'You do not need to allocate your entire budget during this call (although you can, especially if it seems low). '
            f'Scope question ID: `{question_id}`\n\n'
            'Phase 1 (specialized scouts + assess) is complete. Now plan '
            'targeted follow-up based on what was discovered.\n\n'
            f'{scores_text}\n\n'
            'Dispatch further investigation: use dispatch_find_considerations for general '
            'exploration that can be based purely on your trained knowledge and does not require web research, '
            'dispatch_web_research for web-based evidence, or '
            'recurse_into_subquestion to recursively investigate a child '
            'question with its own prioritization cycle. '
            'You can target the parent question or any child question.\n\n'
            'You may create subquestions before dispatching. '
            'You must make all your dispatch calls now — this is your only turn.'
            'CRITICAL: You MUST dispatch at least two recurse_into_subquestion calls if you have enough budget to do so.'
        )

        result = await run_prioritization_call(
            task, context_text, p_call, self.db,
            available_moves=PRIORITIZATION_MOVES,
            subtree_ids=subtree_ids,
            short_id_map=short_id_map,
            trace=trace,
            dispatch_types=list(PHASE2_DISPATCH_TYPES),
            extra_dispatch_defs=[RECURSE_DISPATCH_DEF],
        )

        sequences: list[list[Dispatch]] = []
        children: list[tuple[TwoPhaseOrchestrator, str]] = []
        for d in result.dispatches:
            if isinstance(d.payload, RecurseDispatchPayload):
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
                children.append((child, resolved))
                log.info(
                    'Queued recursive investigation: question=%s, budget=%d — %s',
                    resolved[:8], d.payload.budget, d.payload.reason,
                )
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
            p_call, self.db,
            f'Phase 2 complete. Planned {len(sequences)} concurrent sequences.',
        )

        self._call_id = p_call.id
        self._trace = trace

        log.info(
            'TwoPhaseOrchestrator phase2 complete: %d sequences, %d children',
            len(sequences), len(children),
        )
        return PrioritizationResult(
            dispatch_sequences=sequences,
            call_id=p_call.id,
            trace=trace,
            children=children,
        )


def Orchestrator(db: DB, broadcaster: Broadcaster | None = None) -> BaseOrchestrator:
    """Factory function: returns the appropriate orchestrator subclass."""
    if get_settings().prioritizer_variant == 'two_phase':
        return TwoPhaseOrchestrator(db, broadcaster)
    return LLMOrchestrator(db, broadcaster)
