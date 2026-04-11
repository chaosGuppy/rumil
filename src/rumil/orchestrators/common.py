"""
Shared helpers, data classes, and standalone orchestration functions.
"""

import logging
import math
import os
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pydantic
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from rumil.orchestrators.base import BaseOrchestrator

from rumil.calls.assess_concept_types import (
    SCREENING_FRUIT_THRESHOLD,
    SCREENING_MAX_ROUNDS,
    SCREENING_PHASE,
    VALIDATION_FRUIT_THRESHOLD,
    VALIDATION_MAX_ROUNDS,
    VALIDATION_PHASE,
)
from rumil.calls.assess_concept import AssessConceptCall
from rumil.calls.call_registry import ASSESS_CALL_CLASSES
from rumil.calls.find_considerations import FindConsiderationsCall
from rumil.calls.ingest import IngestCall
from rumil.calls.scout_concepts import ScoutConceptsCall
from rumil.calls.web_research import WebResearchCall
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
    question_id: str,
    graph: PageGraph,
    visited: set[str] | None = None,
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


async def _describe_considerations_on_page(
    page_id: str,
    graph: PageGraph,
) -> tuple[str, str]:
    """Build enriched descriptions of claims and questions linked to a page.

    Returns (claims_text, questions_text) with research stats for each.
    """
    considerations = await graph.get_considerations_for_question(page_id)
    child_questions = await graph.get_child_questions(page_id)

    claim_lines = []
    for page, link in considerations:
        sub_considerations = await graph.get_considerations_for_question(page.id)
        sub_questions = await graph.get_child_questions(page.id)
        parts = []
        if sub_considerations:
            parts.append(f"{len(sub_considerations)} considerations")
        if sub_questions:
            parts.append(
                f"{len(sub_questions)} subquestion{'s' if len(sub_questions) != 1 else ''}"
            )
        stats = ", ".join(parts) if parts else "no research yet"
        direction = link.direction.value if link.direction else "neutral"
        credence_tag = ""
        if page.credence is not None:
            credence_tag = f" C{page.credence}/R{page.robustness or 1}"
        claim_lines.append(
            f"- `{page.id}` — {page.headline} [{direction}{credence_tag}] ({stats})"
        )

    question_lines = []
    for q in child_questions:
        sub_considerations = await graph.get_considerations_for_question(q.id)
        subtree_count = await _count_subtree_questions(q.id, graph)
        parts = []
        if sub_considerations:
            parts.append(f"{len(sub_considerations)} considerations")
        if subtree_count:
            parts.append(
                f"{subtree_count} subquestion{'s' if subtree_count != 1 else ''}"
            )
        stats = ", ".join(parts) if parts else "no research yet"
        question_lines.append(f"- `{q.id}` — {q.headline} ({stats})")

    claims_text = "\n".join(claim_lines) if claim_lines else "(none)"
    questions_text = "\n".join(question_lines) if question_lines else "(none)"
    return claims_text, questions_text


def compute_priority_score(
    impact_on_question: int,
    broader_impact: int,
    fruit: int,
) -> int:
    """Synthetic priority from three scoring dimensions.

    Formula: floor((2 * 2^(ioq/2) + 2^(bi/2)) * fruit / 10)
    """
    raw = (2 * 2 ** (impact_on_question / 2) + 2 ** (broader_impact / 2)) * fruit
    return math.floor(raw / 10)


class SubquestionScore(BaseModel):
    question_id: str = Field(description="Full UUID of the subquestion")
    headline: str = Field(description="Headline of the subquestion")
    impact_on_question: int = Field(
        description="0-10: how much answering this helps the parent question"
    )
    broader_impact: int = Field(
        description=(
            "0-10: how strategically important it is in general to have a "
            "good answer to this question"
        )
    )
    fruit: int = Field(description="0-10: how much useful investigation remains")
    reasoning: str = Field(description="Brief explanation of scores")


class SubquestionScoringResult(BaseModel):
    scores: list[SubquestionScore]


class ClaimScore(BaseModel):
    page_id: str = Field(description="Full UUID of the claim")
    headline: str = Field(description="Headline of the claim")
    impact_on_question: int = Field(
        description="0-10: how much resolving this helps the parent investigation"
    )
    broader_impact: int = Field(
        description=(
            "0-10: how strategically important it is in general to have a "
            "good answer on this claim"
        )
    )
    fruit: int = Field(description="0-10: how much useful investigation remains")
    reasoning: str = Field(description="Brief explanation of scores")


class ClaimScoringResult(BaseModel):
    scores: list[ClaimScore]


class FruitResult(BaseModel):
    """Deprecated: kept for reference. Use PerTypeFruitResult instead."""

    fruit: int = Field(description="0-10: how much useful investigation remains")
    reasoning: str = Field(description="Brief explanation")


SCORING_BATCH_SIZE = 10


def _split_into_batches(n: int, max_per_batch: int) -> list[int]:
    """Split *n* items into balanced batches of at most *max_per_batch*.

    E.g. n=25, max=10 → [9, 8, 8] (3 batches, sizes as even as possible).
    """
    if n == 0:
        return []
    n_batches = math.ceil(n / max_per_batch)
    base, extra = divmod(n, n_batches)
    return [base + (1 if i < extra else 0) for i in range(n_batches)]


def _build_item_block(
    item: Page,
    index: int,
    total: int,
    judgements_by_id: dict[str, list[Page]],
    children_by_id: dict[str, list[Page]] | None = None,
) -> str:
    """Build the text block describing a single item for the scorer."""
    parts = [
        f"### Item {index + 1}/{total}",
        f"ID: `{item.id}`",
        f"Headline: {item.headline}",
    ]
    if item.abstract:
        parts.append(f"\nAbstract:\n{item.abstract}")

    judgements = judgements_by_id.get(item.id, [])
    if judgements:
        latest_j = max(judgements, key=lambda j: j.created_at)
        parts.append(
            f"\nLatest judgement (credence {latest_j.credence}/9, "
            f"robustness {latest_j.robustness}/5):"
        )
        if latest_j.abstract:
            parts.append(latest_j.abstract)
        else:
            parts.append(latest_j.headline)
        if latest_j.fruit_remaining is not None:
            parts.append(
                f"\nPrior fruit_remaining estimate: {latest_j.fruit_remaining}/10"
            )
    else:
        parts.append("\nNo prior assessment.")

    if children_by_id is not None:
        children = children_by_id.get(item.id, [])
        if children:
            parts.append("\nSubquestions:")
            for child in children:
                child_js = judgements_by_id.get(child.id, [])
                if child_js:
                    cj = max(child_js, key=lambda j: j.created_at)
                    parts.append(
                        f"- {child.headline} — judgement: {cj.headline} "
                        f"(robustness {cj.robustness}/5)"
                    )
                else:
                    parts.append(f"- {child.headline} — NO JUDGEMENT")
        else:
            parts.append("\nNo subquestions.")

    return "\n".join(parts)


async def score_items_sequentially(
    parent_page: Page,
    parent_judgement: Page | None,
    items: Sequence[Page],
    system_prompt_name: str,
    response_model: type[BaseModel],
    call_id: str,
    db: DB,
) -> list[dict]:
    """Score items in batched multi-turn cached conversation.

    Items are split into balanced batches of up to SCORING_BATCH_SIZE.
    Each batch is presented as a single user message; the model returns
    a list of scores for that batch.

    Bulk-fetches each item's latest judgement up front via
    ``db.get_judgements_for_questions`` so the per-item formatter doesn't
    need a graph.
    """
    from rumil.llm import (
        LLMExchangeMetadata,
        build_system_prompt,
        structured_call,
    )

    if not items:
        return []

    item_ids = [item.id for item in items]
    children_by_id: dict[str, list[Page]] = {}
    all_child_ids: list[str] = []
    for item in items:
        children = await db.get_child_questions(item.id)
        children_by_id[item.id] = children
        all_child_ids.extend(c.id for c in children)

    judgements_by_id = await db.get_judgements_for_questions(item_ids + all_child_ids)

    batch_response_model = pydantic.create_model(
        f"{response_model.__name__}Batch",
        scores=(
            list[response_model],
            Field(description="One score per item in the batch"),
        ),
    )

    parent_parts = [
        f"Parent: {parent_page.headline}",
        "",
    ]
    if parent_page.abstract:
        parent_parts.append(parent_page.abstract)
        parent_parts.append("")
    if parent_judgement:
        parent_parts.append(
            f"Latest judgement (credence {parent_judgement.credence}/9, "
            f"robustness {parent_judgement.robustness}/5):"
        )
        if parent_judgement.abstract:
            parent_parts.append(parent_judgement.abstract)
        else:
            parent_parts.append(parent_judgement.headline)
        parent_parts.append("")

    parent_context = "\n".join(parent_parts)
    system_prompt = build_system_prompt(system_prompt_name)
    messages: list[dict] = []
    results: list[dict] = []

    batch_sizes = _split_into_batches(len(items), SCORING_BATCH_SIZE)
    offset = 0
    for batch_idx, batch_size in enumerate(batch_sizes):
        batch_items = items[offset : offset + batch_size]
        offset += batch_size

        item_blocks = []
        for j, item in enumerate(batch_items):
            global_idx = sum(batch_sizes[:batch_idx]) + j
            block = _build_item_block(
                item,
                global_idx,
                len(items),
                judgements_by_id,
                children_by_id,
            )
            item_blocks.append(block)

        batch_text = (
            f"## Batch {batch_idx + 1}/{len(batch_sizes)} "
            f"({batch_size} items)\n\n"
            + "\n\n".join(item_blocks)
            + "\n\nScore all items in this batch now."
        )

        if batch_idx == 0:
            user_content = parent_context + "\n" + batch_text
        else:
            user_content = batch_text

        messages.append({"role": "user", "content": user_content})

        result = await structured_call(
            system_prompt,
            messages=list(messages),
            response_model=batch_response_model,
            cache=True,
            metadata=LLMExchangeMetadata(
                call_id=call_id,
                phase=f"score_batch_{batch_idx}",
                user_messages=[{"role": "user", "content": user_content}],
            ),
            db=db,
        )

        response_text = result.response_text or ""
        messages.append({"role": "assistant", "content": response_text})

        if result.parsed:
            parsed_dict = result.parsed.model_dump()
            for score in parsed_dict.get("scores", []):
                results.append(score)

    return results


@dataclass
class PrioritizationResult:
    dispatch_sequences: Sequence[Sequence[Dispatch]]
    call_id: str | None = None
    children: Sequence[tuple["BaseOrchestrator", str]] = ()


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
        log.warning(
            "Failed to create embedding for root question %s",
            page.id[:8],
            exc_info=True,
        )
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
            SMOKE_TEST_MAX_ROUNDS
            if get_settings().is_smoke_test
            else DEFAULT_MAX_ROUNDS
        )
    elif get_settings().is_smoke_test:
        max_rounds = min(max_rounds, SMOKE_TEST_MAX_ROUNDS)
    log.info(
        "find_considerations_until_done: question=%s, max_rounds=%d, fruit_threshold=%d, mode=%s",
        question_id[:8],
        max_rounds,
        fruit_threshold,
        mode.value,
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

    scout = FindConsiderationsCall(
        question_id,
        call,
        db,
        max_rounds=max_rounds,
        fruit_threshold=fruit_threshold,
        mode=mode,
        context_page_ids=context_page_ids,
        broadcaster=broadcaster,
    )
    await scout.run()

    log.info(
        "find_considerations_until_done finished: %d rounds, call=%s",
        scout.rounds_completed,
        call.id[:8],
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
            SMOKE_TEST_INGEST_MAX_ROUNDS
            if get_settings().is_smoke_test
            else DEFAULT_INGEST_MAX_ROUNDS
        )
    log.info(
        "ingest_until_done: source=%s, question=%s, max_rounds=%d",
        source_page.id[:8],
        question_id[:8],
        max_rounds,
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
        ingest = IngestCall(source_page, question_id, call, db, broadcaster=broadcaster)
        await ingest.run()
        review = ingest.review
        rounds += 1

        remaining_fruit = review.get("remaining_fruit", 5) if review else 5
        log.info(
            "Ingest round %d/%d: remaining_fruit=%d (threshold=%d)",
            i + 1,
            max_rounds,
            remaining_fruit,
            fruit_threshold,
        )

        if remaining_fruit <= fruit_threshold:
            log.info(
                "Ingest fruit (%d) below threshold (%d), stopping",
                remaining_fruit,
                fruit_threshold,
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
        question_id,
        db,
        parent_call_id=parent_call_id,
        sequence_id=sequence_id,
        sequence_position=sequence_position,
    )

    assess_position = sequence_position + 1 if sequence_position is not None else None
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
        concept_id[:8],
        phase,
        max_rounds,
        fruit_threshold,
    )
    last_review: dict = {}
    for i in range(max_rounds):
        call = await db.create_call(
            CallType.ASSESS_CONCEPT,
            scope_page_id=concept_id,
            parent_call_id=parent_call_id,
        )
        assess = AssessConceptCall(
            concept_id, call, db, phase=phase, broadcaster=broadcaster
        )
        await assess.run()
        last_review = assess.concept_assessment

        remaining_fruit = last_review.get("remaining_fruit", 10)
        log.info(
            "Assess concept round %d/%d (%s): fruit=%d, score=%s",
            i + 1,
            max_rounds,
            phase,
            remaining_fruit,
            last_review.get("score"),
        )
        if remaining_fruit <= fruit_threshold:
            log.info(
                "Concept fruit (%d) at or below threshold (%d), stopping %s phase",
                remaining_fruit,
                fruit_threshold,
                phase,
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
    scout = ScoutConceptsCall(question_id, scout_call, db, broadcaster=broadcaster)
    await scout.run()
    proposed_ids = scout.result.created_page_ids

    log.info(
        "Scout concepts complete: %d proposals for question=%s",
        len(proposed_ids),
        question_id[:8],
    )

    for concept_id in proposed_ids:
        concept = await db.get_page(concept_id)
        label = concept.headline[:60] if concept else concept_id[:8]
        log.info("Screening concept: %s [%s]", label, concept_id[:8])

        screening_review = await _run_assess_concept_loop(
            concept_id,
            db,
            phase=SCREENING_PHASE,
            max_rounds=SCREENING_MAX_ROUNDS,
            fruit_threshold=SCREENING_FRUIT_THRESHOLD,
            parent_call_id=scout_call.id,
            broadcaster=broadcaster,
        )

        if not screening_review.get("screening_passed"):
            log.info(
                "Concept [%s] did not pass screening (score=%s)",
                concept_id[:8],
                screening_review.get("score"),
            )
            continue

        log.info(
            "Concept [%s] passed screening (score=%s), proceeding to validation",
            concept_id[:8],
            screening_review.get("score"),
        )

        validation_review = await _run_assess_concept_loop(
            concept_id,
            db,
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
                concept_id[:8],
                validation_review.get("score"),
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
    log.info("web_research_question: question=%s", question_id[:8])
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
    web_research = WebResearchCall(
        question_id,
        call,
        db,
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
