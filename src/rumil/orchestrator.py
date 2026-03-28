"""
Orchestrator: drives the research workflow using the prioritization system.
Budget is tracked here; prioritization and review calls are free.
"""

import asyncio
import logging
import os
import uuid
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import BaseModel, Field

from rumil.calls import run_prioritization
from rumil.calls.assess_concept_types import (
    SCREENING_FRUIT_THRESHOLD,
    SCREENING_MAX_ROUNDS,
    SCREENING_PHASE,
    VALIDATION_FRUIT_THRESHOLD,
    VALIDATION_MAX_ROUNDS,
    VALIDATION_PHASE,
)
from rumil.calls.common import mark_call_completed
from rumil.calls.dispatches import DISPATCH_DEFS, DispatchDef, RECURSE_DISPATCH_DEF
from rumil.calls.prioritization import run_prioritization_call
from rumil.calls.summarize import summarize_question
from rumil.calls.call_registry import (
    ASSESS_CALL_CLASSES,
    ASSESS_CONCEPT_CALL_CLASSES,
    INGEST_CALL_CLASSES,
    FIND_CONSIDERATIONS_CALL_CLASSES,
    SCOUT_CONCEPTS_CALL_CLASSES,
    SCOUT_ANALOGIES_CALL_CLASSES,
    SCOUT_DEEP_QUESTIONS_CALL_CLASSES,
    SCOUT_FACTCHECKS_CALL_CLASSES,
    SCOUT_PARADIGM_CASES_CALL_CLASSES,
    SCOUT_WEB_QUESTIONS_CALL_CLASSES,
    SCOUT_ESTIMATES_CALL_CLASSES,
    SCOUT_HYPOTHESES_CALL_CLASSES,
    SCOUT_SUBQUESTIONS_CALL_CLASSES,
    WEB_RESEARCH_CALL_CLASSES,
)
from rumil.context import build_prioritization_context, collect_subtree_ids
from rumil.database import DB
from rumil.embeddings import embed_and_store_page
from rumil.llm import LLMExchangeMetadata, build_system_prompt, build_user_message, structured_call
from rumil.constants import (
    DEFAULT_FRUIT_THRESHOLD,
    DEFAULT_INGEST_FRUIT_THRESHOLD,
    DEFAULT_INGEST_MAX_ROUNDS,
    DEFAULT_MAX_ROUNDS,
    MIN_TWOPHASE_BUDGET,
    SMOKE_TEST_INGEST_MAX_ROUNDS,
    SMOKE_TEST_MAX_ROUNDS,
)
from rumil.models import (
    AssessDispatchPayload,
    Call,
    CallType,
    Dispatch,
    LinkType,
    MoveType,
    Page,
    PageLayer,
    PageType,
    PrioritizationDispatchPayload,
    RecurseDispatchPayload,
    ScoutAnalogiesDispatchPayload,
    ScoutDispatchPayload,
    ScoutDeepQuestionsDispatchPayload,
    ScoutFactchecksDispatchPayload,
    FindConsiderationsMode,
    ScoutWebQuestionsDispatchPayload,
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
from rumil.tracing.tracer import CallTrace, get_trace, set_trace
from rumil.tracing.trace_events import (
    CallTypeFruitScoreItem,
    ContextBuiltEvent,
    DispatchExecutedEvent,
    DispatchesPlannedEvent,
    DispatchTraceItem,
    ErrorEvent,
    ScoringCompletedEvent,
    SubquestionScoreItem,
)


log = logging.getLogger(__name__)


async def _count_subtree_questions(
    question_id: str, graph: PageGraph, visited: set[str] | None = None,
) -> int:
    """Count all descendant questions (not including the question itself)."""
    if visited is None:
        visited = set()
    visited.add(question_id)
    children = await graph.get_child_questions(question_id)
    count = 0
    for child in children:
        if child.id in visited:
            continue
        count += 1
        count += await _count_subtree_questions(child.id, graph, visited)
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
        if considerations:
            parts.append(f'{len(considerations)} considerations')
        if judgements:
            parts.append(f'{len(judgements)} judgement{"s" if len(judgements) != 1 else ""}')
        if subtree_count:
            parts.append(f'{subtree_count} subquestion{"s" if subtree_count != 1 else ""}')

        stats = ', '.join(parts) if parts else 'no research yet'
        lines.append(f'- `{c.id}` — {c.headline} ({stats})')
    return '\n'.join(lines)


PRIORITIZATION_MOVES: list[MoveType] = [
    MoveType.CREATE_QUESTION,
    MoveType.LINK_CHILD_QUESTION,
]

from rumil.available_calls import get_available_calls_preset


class SubquestionScore(BaseModel):
    question_id: str = Field(description='Full UUID of the subquestion')
    headline: str = Field(description='Headline of the subquestion')
    impact: int = Field(description='0-10: how much answering this helps the parent')
    fruit: int = Field(description='0-10: how much useful investigation remains')
    reasoning: str = Field(description='Brief explanation of scores')


class SubquestionScoringResult(BaseModel):
    scores: list[SubquestionScore]


class FruitResult(BaseModel):
    """Deprecated: kept for reference. Use PerTypeFruitResult instead."""
    fruit: int = Field(description='0-10: how much useful investigation remains')
    reasoning: str = Field(description='Brief explanation')


class CallTypeFruitScore(BaseModel):
    call_type: str = Field(
        description='e.g. development, scout_subquestions, scout_estimates'
    )
    fruit: int = Field(description='0-10: how much useful work of this type remains')
    reasoning: str = Field(description='Brief explanation')


class PerTypeFruitResult(BaseModel):
    scores: list[CallTypeFruitScore]


_LOW = 2
_HIGH = 6


def compute_dispatch_guidance(
    fruit_scores: Sequence[CallTypeFruitScore],
) -> str:
    """Produce dispatch guidance text from per-scout-type fruit scores."""
    dev_score: int | None = None
    scout_scores: dict[str, int] = {}
    for s in fruit_scores:
        if s.call_type == 'development':
            dev_score = s.fruit
        else:
            scout_scores[s.call_type] = s.fruit

    if dev_score is None:
        dev_score = 5

    high_scouts = [k for k, v in scout_scores.items() if v >= _HIGH]
    low_scouts = [k for k, v in scout_scores.items() if v <= _LOW]
    exhausted_scouts = [k for k, v in scout_scores.items() if v <= 1]
    all_scouts_low = len(low_scouts) == len(scout_scores) and len(scout_scores) > 0
    any_scouts_moderate_or_high = any(
        v > _LOW for v in scout_scores.values()
    )

    lines: list[str] = []

    if dev_score <= _LOW and high_scouts:
        lines.append(
            'Development avenues are well-explored. Focus budget on scouting: '
            + ', '.join(high_scouts) + '.'
        )
    elif dev_score >= _HIGH and all_scouts_low:
        lines.append(
            'Scouting is largely exhausted. Focus budget on developing '
            'existing subquestions via find_considerations and recurse.'
        )
    elif dev_score >= _HIGH and high_scouts:
        lines.append(
            'Both development and scouting have significant remaining fruit. '
            'Allocate broadly across development and high-fruit scouts: '
            + ', '.join(high_scouts) + '.'
        )
    elif dev_score > _LOW and any_scouts_moderate_or_high:
        names = [k for k, v in scout_scores.items() if v > _LOW]
        lines.append(
            'Balance budget between development calls and high-fruit scouts: '
            + ', '.join(names) + '.'
        )
    elif dev_score > _LOW and all_scouts_low:
        lines.append(
            'Scouting is largely exhausted but development has moderate fruit. '
            'Focus budget on developing existing subquestions.'
        )
    elif dev_score <= _LOW and all_scouts_low:
        lines.append(
            'Remaining fruit is low across the board. '
            'Consider allocating conservatively.'
        )

    if not all_scouts_low:
        for name in exhausted_scouts:
            lines.append(f'{name} appears exhausted — avoid dispatching.')

    return '\n'.join(lines)


@dataclass
class PrioritizationResult:
    dispatch_sequences: Sequence[Sequence[Dispatch]]
    call_id: str | None = None
    children: Sequence[tuple['TwoPhaseOrchestrator', str]] = ()


async def create_root_question(
    question_text: str,
    db: DB,
    *,
    abstract: str = "",
    content: str = "",
) -> str:
    page = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=content or abstract or question_text,
        headline=question_text,
        abstract=abstract,
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
    mode: FindConsiderationsMode = FindConsiderationsMode.ALTERNATE,
    broadcaster=None,
    force: bool = False,
    call_id: str | None = None,
    sequence_id: str | None = None,
    sequence_position: int | None = None,
) -> tuple[int, list[str]]:
    """Run a cache-aware find-considerations session.

    Creates one Call and delegates to the FindConsiderationsCall class, which handles
    multi-round looping with conversation resumption, lightweight fruit
    checks, and a single closing review at the end.

    Returns (rounds_made, list_of_call_ids).
    """
    if max_rounds is None:
        max_rounds = (
            SMOKE_TEST_MAX_ROUNDS if get_settings().is_smoke_test
            else DEFAULT_MAX_ROUNDS
        )
    elif get_settings().is_smoke_test:
        max_rounds = min(max_rounds, SMOKE_TEST_MAX_ROUNDS)
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
        call_id=call_id,
        sequence_id=sequence_id,
        sequence_position=sequence_position,
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
    call_id: str | None = None,
    sequence_id: str | None = None,
    sequence_position: int | None = None,
) -> str | None:
    """Run one Assess call on a question. Returns call ID, or None if no budget.

    When ``sequence_id`` is provided, the summarise call is placed at
    ``sequence_position`` and the assess call at ``sequence_position + 1``.
    Callers should account for two positions being consumed.
    """
    log.info("assess_question: question=%s", question_id[:8])
    if not await _consume_budget(db, force=force):
        return None

    await summarize_question(
        question_id, db,
        parent_call_id=parent_call_id,
        sequence_id=sequence_id,
        sequence_position=sequence_position,
    )

    assess_position = (
        sequence_position + 1 if sequence_position is not None else None
    )
    call = await db.create_call(
        CallType.ASSESS,
        scope_page_id=question_id,
        parent_call_id=parent_call_id,
        context_page_ids=context_page_ids,
        call_id=call_id,
        sequence_id=sequence_id,
        sequence_position=assess_position,
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
    call_id: str | None = None,
    sequence_id: str | None = None,
    sequence_position: int | None = None,
) -> str | None:
    """Run one web research call on a question. Returns call ID, or None if no budget."""
    log.info('web_research_question: question=%s', question_id[:8])
    if not await _consume_budget(db, force=force):
        return None

    call = await db.create_call(
        CallType.WEB_RESEARCH,
        scope_page_id=question_id,
        parent_call_id=parent_call_id,
        call_id=call_id,
        sequence_id=sequence_id,
        sequence_position=sequence_position,
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
        call_id: str | None = None,
        sequence_id: str | None = None,
        sequence_position: int | None = None,
        max_rounds: int = 5,
        fruit_threshold: int = 4,
    ) -> str | None:
        """Run a call dispatch with optional multi-round support.

        Budget consumption is handled internally by MultiRoundLoop
        (one unit per round), matching how find_considerations works.
        """
        if get_settings().is_smoke_test:
            max_rounds = min(max_rounds, SMOKE_TEST_MAX_ROUNDS)

        if force and await self.db.budget_remaining() <= 0:
            await self.db.add_budget(1)

        call = await self.db.create_call(
            call_type,
            scope_page_id=question_id,
            parent_call_id=parent_call_id,
            call_id=call_id,
            sequence_id=sequence_id,
            sequence_position=sequence_position,
        )
        cls = registry['default']
        instance = cls(
            question_id, call, self.db,
            broadcaster=self.broadcaster,
            max_rounds=max_rounds,
            fruit_threshold=fruit_threshold,
        )
        await instance.run()
        return call.id

    async def _run_dispatch_sequence(
        self,
        sequence: Sequence[Dispatch],
        scope_question_id: str,
        parent_call_id: str | None,
        base_index: int,
        position_in_batch: int = 0,
    ) -> bool:
        """Run dispatches in a sequence sequentially. Returns True if any executed.

        All dispatches in the sequence are guaranteed to run: if budget
        is exhausted mid-sequence, subsequent dispatches force-consume
        so that trailing calls (e.g. auto-assess) are never skipped.

        Child call IDs are pre-generated so that DispatchExecutedEvents
        can be recorded before execution begins, making dispatch links
        clickable in the trace frontend immediately.
        """
        is_multi_step = len(sequence) > 1
        seq_id: str | None = None
        if is_multi_step:
            call_sequence = await self.db.create_call_sequence(
                parent_call_id=parent_call_id,
                scope_question_id=scope_question_id,
                position_in_batch=position_in_batch,
            )
            seq_id = call_sequence.id

        pre_ids = [str(uuid.uuid4()) for _ in sequence]
        resolves = []
        headlines = []
        for dispatch in sequence:
            resolved = await self.db.resolve_page_id(dispatch.payload.question_id)
            resolved = resolved or scope_question_id
            resolves.append(resolved)
            page = await self.db.get_page(resolved)
            headlines.append(page.headline if page else '')

        trace = get_trace()
        if trace:
            for i, dispatch in enumerate(sequence):
                await trace.record(DispatchExecutedEvent(
                    index=base_index + i,
                    child_call_type=dispatch.call_type.value,
                    question_id=resolves[i],
                    question_headline=headlines[i],
                    child_call_id=pre_ids[i],
                ))

        executed = False
        for i, dispatch in enumerate(sequence):
            force = i > 0 and await self.db.budget_remaining() <= 0
            await self._execute_dispatch(
                dispatch, scope_question_id, parent_call_id,
                force=force, call_id=pre_ids[i],
                sequence_id=seq_id, sequence_position=i if is_multi_step else None,
            )
            executed = True
        return executed

    async def _execute_dispatch(
        self,
        dispatch: Dispatch,
        scope_question_id: str,
        parent_call_id: str | None,
        *,
        force: bool = False,
        call_id: str | None = None,
        sequence_id: str | None = None,
        sequence_position: int | None = None,
    ) -> tuple[str, str | None]:
        """Execute a single dispatch.

        When *force* is True, budget is expanded if needed so the call
        always proceeds (used for trailing dispatches in a committed batch).

        When *call_id* is provided, the child call will be created with
        that ID (for eager link creation in traces).

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
                call_id=call_id,
                sequence_id=sequence_id,
                sequence_position=sequence_position,
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
                call_id=call_id,
                sequence_id=sequence_id,
                sequence_position=sequence_position,
            )

        elif isinstance(p, ScoutSubquestionsDispatchPayload):
            log.info('Dispatch: scout_subquestions on %s (max_rounds=%d) — %s', d_label, p.max_rounds, p.reason)
            child_call_id = await self._run_simple_call_dispatch(
                resolved, CallType.SCOUT_SUBQUESTIONS,
                SCOUT_SUBQUESTIONS_CALL_CLASSES, parent_call_id,
                force=force, call_id=call_id,
                sequence_id=sequence_id, sequence_position=sequence_position,
                max_rounds=p.max_rounds, fruit_threshold=p.fruit_threshold,
            )

        elif isinstance(p, ScoutEstimatesDispatchPayload):
            log.info('Dispatch: scout_estimates on %s (max_rounds=%d) — %s', d_label, p.max_rounds, p.reason)
            child_call_id = await self._run_simple_call_dispatch(
                resolved, CallType.SCOUT_ESTIMATES,
                SCOUT_ESTIMATES_CALL_CLASSES, parent_call_id,
                force=force, call_id=call_id,
                sequence_id=sequence_id, sequence_position=sequence_position,
                max_rounds=p.max_rounds, fruit_threshold=p.fruit_threshold,
            )

        elif isinstance(p, ScoutHypothesesDispatchPayload):
            log.info('Dispatch: scout_hypotheses on %s (max_rounds=%d) — %s', d_label, p.max_rounds, p.reason)
            child_call_id = await self._run_simple_call_dispatch(
                resolved, CallType.SCOUT_HYPOTHESES,
                SCOUT_HYPOTHESES_CALL_CLASSES, parent_call_id,
                force=force, call_id=call_id,
                sequence_id=sequence_id, sequence_position=sequence_position,
                max_rounds=p.max_rounds, fruit_threshold=p.fruit_threshold,
            )

        elif isinstance(p, ScoutAnalogiesDispatchPayload):
            log.info('Dispatch: scout_analogies on %s (max_rounds=%d) — %s', d_label, p.max_rounds, p.reason)
            child_call_id = await self._run_simple_call_dispatch(
                resolved, CallType.SCOUT_ANALOGIES,
                SCOUT_ANALOGIES_CALL_CLASSES, parent_call_id,
                force=force, call_id=call_id,
                sequence_id=sequence_id, sequence_position=sequence_position,
                max_rounds=p.max_rounds, fruit_threshold=p.fruit_threshold,
            )

        elif isinstance(p, ScoutParadigmCasesDispatchPayload):
            log.info('Dispatch: scout_paradigm_cases on %s (max_rounds=%d) — %s', d_label, p.max_rounds, p.reason)
            child_call_id = await self._run_simple_call_dispatch(
                resolved, CallType.SCOUT_PARADIGM_CASES,
                SCOUT_PARADIGM_CASES_CALL_CLASSES, parent_call_id,
                force=force, call_id=call_id,
                sequence_id=sequence_id, sequence_position=sequence_position,
                max_rounds=p.max_rounds, fruit_threshold=p.fruit_threshold,
            )

        elif isinstance(p, ScoutFactchecksDispatchPayload):
            log.info('Dispatch: scout_factchecks on %s (max_rounds=%d) — %s', d_label, p.max_rounds, p.reason)
            child_call_id = await self._run_simple_call_dispatch(
                resolved, CallType.SCOUT_FACTCHECKS,
                SCOUT_FACTCHECKS_CALL_CLASSES, parent_call_id,
                force=force, call_id=call_id,
                sequence_id=sequence_id, sequence_position=sequence_position,
                max_rounds=p.max_rounds, fruit_threshold=p.fruit_threshold,
            )

        elif isinstance(p, ScoutWebQuestionsDispatchPayload):
            log.info('Dispatch: scout_web_questions on %s (max_rounds=%d) — %s', d_label, p.max_rounds, p.reason)
            child_call_id = await self._run_simple_call_dispatch(
                resolved, CallType.SCOUT_WEB_QUESTIONS,
                SCOUT_WEB_QUESTIONS_CALL_CLASSES, parent_call_id,
                force=force, call_id=call_id,
                sequence_id=sequence_id, sequence_position=sequence_position,
                max_rounds=p.max_rounds, fruit_threshold=p.fruit_threshold,
            )

        elif isinstance(p, ScoutDeepQuestionsDispatchPayload):
            log.info('Dispatch: scout_deep_questions on %s (max_rounds=%d) — %s', d_label, p.max_rounds, p.reason)
            child_call_id = await self._run_simple_call_dispatch(
                resolved, CallType.SCOUT_DEEP_QUESTIONS,
                SCOUT_DEEP_QUESTIONS_CALL_CLASSES, parent_call_id,
                force=force, call_id=call_id,
                sequence_id=sequence_id, sequence_position=sequence_position,
                max_rounds=p.max_rounds, fruit_threshold=p.fruit_threshold,
            )

        elif isinstance(p, WebResearchDispatchPayload):
            log.info('Dispatch: web_research on %s — %s', d_label, p.reason)
            child_call_id = await web_research_question(
                resolved, self.db,
                parent_call_id=parent_call_id,
                broadcaster=self.broadcaster,
                force=force,
                call_id=call_id,
                sequence_id=sequence_id,
                sequence_position=sequence_position,
            )

        return resolved, child_call_id

    async def _run_sequences(
        self,
        sequences: Sequence[Sequence[Dispatch]],
        scope_question_id: str,
        call_id: str | None,
    ) -> bool:
        """Run multiple dispatch sequences concurrently. Returns True if any executed."""
        base_index = 0
        tasks = []
        for batch_pos, seq in enumerate(sequences):
            tasks.append(self._run_dispatch_sequence(
                seq, scope_question_id, call_id,
                base_index,
                position_in_batch=batch_pos,
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
                    result.call_id,
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
                        mode=get_settings().allowed_find_considerations_modes[0],
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
        phase1_budget = min(budget - 3, MIN_TWOPHASE_BUDGET)
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
                f'TwoPhaseOrchestrator requires a budget of at least '
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

                result = await self._get_next_batch(root_question_id, effective)
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
                                    f"Concurrent dispatch failed: "
                                    f"{type(r).__name__}: {r}"
                                ),
                                phase="dispatch",
                            ))

                if not any(not isinstance(r, Exception) for r in results):
                    break

                self._executed_since_last_plan = True

                if self._invocation > 1:
                    await assess_question(
                        root_question_id, self.db,
                        parent_call_id=self._parent_call_id,
                        broadcaster=self.broadcaster, force=True,
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
            sequence, scope_question_id, parent_call_id, base_index,
            position_in_batch=position_in_batch,
        )
        if result:
            self._consumed += len(sequence)
        return result

    async def _is_new_question(self, question_id: str) -> bool:
        """A question is 'new' if it has no links besides child_question to a parent."""
        links = await self.db.get_links_to(question_id)
        return all(l.link_type == LinkType.CHILD_QUESTION for l in links)

    async def _get_next_batch(
        self,
        question_id: str,
        budget: int,
        parent_call_id: str | None = None,
    ) -> PrioritizationResult:
        if self._invocation == 0:
            self._invocation += 1
            if await self._is_new_question(question_id):
                return await self._phase1(question_id, budget, parent_call_id)
            self._executed_since_last_plan = True

        if not self._executed_since_last_plan:
            return PrioritizationResult(dispatch_sequences=[])

        self._executed_since_last_plan = False
        self._invocation += 1
        return await self._phase2(question_id, budget, self._parent_call_id)

    async def _phase1(
        self,
        question_id: str,
        budget: int,
        parent_call_id: str | None,
    ) -> PrioritizationResult:
        phase1_budget = min(budget - 3, MIN_TWOPHASE_BUDGET)
        log.info(
            'TwoPhaseOrchestrator phase1: question=%s, budget=%d, phase1_budget=%d',
            question_id[:8], budget, phase1_budget,
        )

        graph = await PageGraph.load(self.db)
        context_text, short_id_map = await build_prioritization_context(
            self.db, scope_question_id=question_id, graph=graph,
        )
        subtree_ids = await collect_subtree_ids(question_id, self.db, graph=graph)
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

        task = (
            f'You have a budget of **{phase1_budget} research calls** to distribute '
            'among the dispatch tools below.\n\n'
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

            subtree_ids=subtree_ids,
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
            sequence_id=self._sequence_id,
            sequence_position=self._seq_position if self._sequence_id else None,
        )
        if self._sequence_id is not None:
            self._seq_position += 1
        trace = CallTrace(p_call.id, self.db, broadcaster=self.broadcaster)
        set_trace(trace)
        await trace.record(ContextBuiltEvent(budget=budget))

        graph = await PageGraph.load(self.db)
        child_questions = await graph.get_child_questions(question_id)
        parent_question = await graph.get_page(question_id)
        if not parent_question:
            raise RuntimeError(
                f'Parent question {question_id} not found in PageGraph. '
                'This usually means the question belongs to a different project '
                'than the current DB scope.'
            )
        parent_headline = parent_question.headline

        scoring_system = build_system_prompt('score_subquestions')

        scoring_tasks = []
        if child_questions:
            child_descriptions = await _describe_child_questions(child_questions, graph)
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
                ),
                db=self.db,
            ))
        else:
            async def _empty_scores():
                return type('R', (), {'data': {'scores': []}})()
            scoring_tasks.append(_empty_scores())

        preset = get_available_calls_preset()
        scout_types = [
            ct for ct in preset.phase2_dispatch
            if ct.value.startswith('scout_')
        ]
        type_desc_lines = [
            '- **development**: Deeper investigation of existing subquestions '
            'via find_considerations, web_research, and recursion.',
        ]
        for ct in scout_types:
            ddef = DISPATCH_DEFS.get(ct)
            if ddef:
                type_desc_lines.append(f'- **{ct.value}**: {ddef.description}')
        type_descriptions = '\n'.join(type_desc_lines)

        call_counts = await self.db.get_call_counts_by_type(question_id)
        history_lines = [f'- {ct}: {n} call(s)' for ct, n in call_counts.items()]
        history_text = (
            'Prior completed calls on this question:\n'
            + ('\n'.join(history_lines) if history_lines else '(none)')
        )

        fruit_system = build_system_prompt('score_per_type_fruit')
        fruit_user_msg = build_user_message(
            f'Question: {parent_headline}\n\n'
            f'Question ID: `{question_id}`\n\n'
            f'{history_text}\n\n'
            f'## Call types to score\n\n{type_descriptions}',
            'Score the remaining fruit for each call type listed. '
            'Return one score per call type.',
        )
        scoring_tasks.append(structured_call(
            fruit_system,
            user_message=fruit_user_msg,
            response_model=PerTypeFruitResult,
            metadata=LLMExchangeMetadata(
                call_id=p_call.id,
                phase='score_per_type_fruit',
            ),
            db=self.db,
        ))

        scoring_results = await asyncio.gather(*scoring_tasks)
        subq_result = scoring_results[0]
        fruit_result = scoring_results[1]

        subq_scores = subq_result.data.get('scores', []) if subq_result.data else []
        raw_fruit_scores = fruit_result.data.get('scores', []) if fruit_result.data else []
        per_type_scores = [CallTypeFruitScore(**s) for s in raw_fruit_scores]

        has_dev_score = any(s.call_type == 'development' for s in per_type_scores)
        if not has_dev_score:
            log.warning(
                'LLM did not return a development fruit score; defaulting to 5'
            )
            await trace.record(ErrorEvent(
                message='LLM omitted development fruit score; defaulting to 5',
                phase='score_per_type_fruit',
            ))

        guidance = compute_dispatch_guidance(per_type_scores)

        await trace.record(ScoringCompletedEvent(
            subquestion_scores=[
                SubquestionScoreItem(**s) for s in subq_scores
            ],
            per_type_fruit=[
                CallTypeFruitScoreItem(
                    call_type=s.call_type, fruit=s.fruit, reasoning=s.reasoning,
                )
                for s in per_type_scores
            ],
            dispatch_guidance=guidance,
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

        fruit_lines = ['## Per-Scout-Type Fruit Scores', '']
        for s in per_type_scores:
            fruit_lines.append(
                f'- **{s.call_type}**: {s.fruit}/10 — {s.reasoning}'
            )
        fruit_lines.append('')
        scores_text += '\n'.join(fruit_lines)

        if guidance:
            scores_text += f'\n## Dispatch Guidance\n\n{guidance}\n'

        context_text, short_id_map = await build_prioritization_context(
            self.db, scope_question_id=question_id, graph=graph,
        )
        subtree_ids = await collect_subtree_ids(question_id, self.db, graph=graph)

        task = (
            f'You have a budget of **{budget} budget units** to allocate.\n\n'
            f'Scope question ID: `{question_id}`\n\n'
            f'{scores_text}\n\n'
            'You must make all your dispatch calls now — this is your only turn. '
            f'Each recurse call must have a budget of at least {MIN_TWOPHASE_BUDGET}.'
        )
        if get_settings().force_twophase_recurse:
            task += (
                '\n\nCRITICAL: You MUST dispatch two recurse calls '
                'if you have enough budget to do so.'
            )

        extra_defs: list[DispatchDef] = []
        if budget >= MIN_TWOPHASE_BUDGET:
            extra_defs.append(RECURSE_DISPATCH_DEF)

        result = await run_prioritization_call(
            task, context_text, p_call, self.db,

            subtree_ids=subtree_ids,
            short_id_map=short_id_map,
            dispatch_types=list(get_available_calls_preset().phase2_dispatch),
            extra_dispatch_defs=extra_defs or None,
            system_prompt_override=build_system_prompt('two_phase_p2'),
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
            if isinstance(d.payload, RecurseDispatchPayload):
                all_trace_items.append(DispatchTraceItem(
                    call_type='recurse',
                    **d.payload.model_dump(exclude_defaults=True),
                ))
        await trace.record(DispatchesPlannedEvent(dispatches=all_trace_items))

        recurse_base = len(all_dispatches)
        for ci, (child, child_qid) in enumerate(children):
            child_call_id = await child.create_initial_call(
                child_qid, parent_call_id=p_call.id,
            )
            child_page = await self.db.get_page(child_qid)
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


def Orchestrator(db: DB, broadcaster: Broadcaster | None = None) -> BaseOrchestrator:
    """Factory function: returns the appropriate orchestrator subclass."""
    if get_settings().prioritizer_variant == 'two_phase':
        return TwoPhaseOrchestrator(db, broadcaster)
    return LLMOrchestrator(db, broadcaster)
