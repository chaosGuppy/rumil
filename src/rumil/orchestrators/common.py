"""
Shared helpers, data classes, and standalone orchestration functions.
"""

import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from rumil.orchestrators.base import BaseOrchestrator

from rumil.calls import run_prioritization
from rumil.calls.assess_concept_types import (
    SCREENING_FRUIT_THRESHOLD,
    SCREENING_MAX_ROUNDS,
    SCREENING_PHASE,
    VALIDATION_FRUIT_THRESHOLD,
    VALIDATION_MAX_ROUNDS,
    VALIDATION_PHASE,
)
from rumil.calls.call_registry import (
    ASSESS_CALL_CLASSES,
    ASSESS_CONCEPT_CALL_CLASSES,
    FIND_CONSIDERATIONS_CALL_CLASSES,
    INGEST_CALL_CLASSES,
    SCOUT_CONCEPTS_CALL_CLASSES,
    WEB_RESEARCH_CALL_CLASSES,
)
from rumil.calls.summarize import summarize_question
from rumil.constants import (
    DEFAULT_FRUIT_THRESHOLD,
    DEFAULT_INGEST_FRUIT_THRESHOLD,
    DEFAULT_INGEST_MAX_ROUNDS,
    DEFAULT_MAX_ROUNDS,
    SMOKE_TEST_INGEST_MAX_ROUNDS,
    SMOKE_TEST_MAX_ROUNDS,
)
from rumil.database import DB
from rumil.embeddings import embed_and_store_page
from rumil.models import (
    CallType,
    Dispatch,
    FindConsiderationsMode,
    MoveType,
    Page,
    PageLayer,
    PageType,
    Workspace,
)
from rumil.page_graph import PageGraph
from rumil.settings import get_settings
from rumil.tracing.broadcast import Broadcaster


log = logging.getLogger(__name__)


PRIORITIZATION_MOVES: list[MoveType] = [
    MoveType.CREATE_QUESTION,
    MoveType.LINK_CHILD_QUESTION,
]


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
    children: Sequence[tuple['BaseOrchestrator', str]] = ()


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
