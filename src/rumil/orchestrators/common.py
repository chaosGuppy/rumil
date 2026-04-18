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

from rumil.calls.adversarial_review import (
    AdversarialReviewCall,
    AdversarialVerdict,
    is_verdict_expired,
)
from rumil.calls.call_registry import ASSESS_CALL_CLASSES
from rumil.calls.find_considerations import FindConsiderationsCall
from rumil.calls.ingest import IngestCall
from rumil.calls.summarize import summarize_question
from rumil.calls.web_research import WebResearchCall
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
    Call,
    CallStatus,
    CallType,
    Dispatch,
    FindConsiderationsMode,
    LinkType,
    MoveType,
    Page,
    PageLayer,
    PageType,
    Workspace,
)
from rumil.question_triage import auto_triage_and_save
from rumil.settings import get_settings
from rumil.tracing.broadcast import Broadcaster

log = logging.getLogger(__name__)


PRIORITIZATION_MOVES: list[MoveType] = [
    MoveType.CREATE_QUESTION,
    MoveType.LINK_CHILD_QUESTION,
]


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
            "0-10: how strategically important it is in general to have a good answer on this claim"
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
            parts.append(f"\nPrior fruit_remaining estimate: {latest_j.fruit_remaining}/10")
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
    question_items = [i for i in items if i.page_type == PageType.QUESTION]
    question_ids = [q.id for q in question_items]

    children_by_id: dict[str, list[Page]] | None = None
    all_child_ids: list[str] = []
    if question_ids:
        children_by_id = {}
        links_by_parent = await db.get_links_from_many(question_ids)
        child_page_ids: list[str] = []
        for qid in question_ids:
            child_page_ids.extend(
                l.to_page_id
                for l in links_by_parent.get(qid, [])
                if l.link_type == LinkType.CHILD_QUESTION
            )
        if child_page_ids:
            child_pages = await db.get_pages_by_ids(child_page_ids)
            for qid in question_ids:
                children_by_id[qid] = [
                    child_pages[l.to_page_id]
                    for l in links_by_parent.get(qid, [])
                    if l.link_type == LinkType.CHILD_QUESTION
                    and l.to_page_id in child_pages
                    and child_pages[l.to_page_id].is_active()
                ]
            all_child_ids = [p.id for p in child_pages.values() if p.is_active()]
        else:
            for qid in question_ids:
                children_by_id[qid] = []

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

    view = await db.get_view_for_question(parent_page.id)
    if view:
        from rumil.context import render_view

        view_items = await db.get_view_items(view.id, min_importance=2)
        view_text = await render_view(view, view_items, min_importance=2)
        if view_text.strip():
            parent_parts.append(view_text)
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

        user_content = parent_context + "\n" + batch_text if batch_idx == 0 else batch_text

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
    task_shape: dict | None = None,
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
        task_shape=task_shape,
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
    if task_shape is None:
        from rumil.task_shape import auto_tag_and_save

        await auto_tag_and_save(page.id, question_text, abstract or content, db)
    await auto_triage_and_save(db, page.id, parent_id=None)
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
        max_rounds = SMOKE_TEST_MAX_ROUNDS if get_settings().is_smoke_test else DEFAULT_MAX_ROUNDS
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

    After the assess completes, if ``settings.enable_adversarial_review`` is
    True, the assessed page's latest judgement has credence >= the configured
    threshold, and the page has not already been adversarially reviewed, an
    adversarial review is dispatched. The review is budget-neutral (it does
    not consume research budget). Failures are logged and swallowed so the
    assess result is not affected.
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

    try:
        await _maybe_adversarial_review_after_assess(
            question_id,
            call,
            db,
            broadcaster=broadcaster,
        )
    except Exception:
        log.warning(
            "assess_question: adversarial-review gate raised for %s — continuing",
            question_id[:8],
            exc_info=True,
        )

    return call.id


async def _maybe_adversarial_review_after_assess(
    question_id: str,
    assess_call: Call,
    db: DB,
    broadcaster: Broadcaster | None = None,
) -> None:
    """Post-assess gate: fire adversarial review when the assessed page's
    latest judgement crosses the credence threshold.

    Shared by all orchestrators via ``assess_question``. Budget-neutral.
    Skipped when the feature is disabled, the page has no judgement, the
    judgement's credence is below threshold, or the page has already been
    adversarially reviewed (``has_adversarial_review``).
    """
    settings = get_settings()
    if not settings.enable_adversarial_review:
        return
    threshold = settings.adversarial_review_credence_threshold

    judgements = await db.get_judgements_for_question(question_id)
    if not judgements:
        return
    latest = max(judgements, key=lambda j: j.created_at)
    if latest.credence is None or latest.credence < threshold:
        return

    if await has_adversarial_review(db, question_id):
        log.info(
            "adversarial-review gate: page %s already reviewed, skipping",
            question_id[:8],
        )
        return

    log.info(
        "adversarial-review gate: firing review for %s (credence=%d)",
        question_id[:8],
        latest.credence,
    )
    verdict = await adversarially_review_claim(
        db,
        assess_call,
        question_id,
        broadcaster=broadcaster,
    )
    if verdict is not None and not verdict.claim_holds:
        log.info(
            "adversarial-review gate: verdict says %s does NOT hold "
            "(stronger_side=%s, confidence=%d)",
            question_id[:8],
            verdict.stronger_side,
            verdict.confidence,
        )


async def adversarially_review_claim(
    db: DB,
    parent_call: Call,
    target_page_id: str,
    broadcaster: Broadcaster | None = None,
) -> AdversarialVerdict | None:
    """Run an adversarial review on target_page_id and return the verdict.

    Budget-neutral — adversarial review is a review call, not a research call.
    Returns None if no verdict was produced (e.g., scout failure).
    """
    log.info(
        "adversarially_review_claim: target=%s, parent_call=%s",
        target_page_id[:8],
        parent_call.id[:8],
    )
    call = await db.create_call(
        CallType.ADVERSARIAL_REVIEW,
        scope_page_id=target_page_id,
        parent_call_id=parent_call.id,
    )
    runner = AdversarialReviewCall(
        target_page_id,
        call,
        db,
        broadcaster=broadcaster,
    )
    try:
        await runner.run()
    except Exception:
        log.warning(
            "adversarially_review_claim: run failed for target %s",
            target_page_id[:8],
            exc_info=True,
        )
        return None

    refreshed = await db.get_call(call.id)
    if refreshed is None or refreshed.status != CallStatus.COMPLETE:
        return None

    verdict_rows = await db._execute(
        db.client.table("pages")
        .select("extra")
        .eq("provenance_call_id", call.id)
        .eq("page_type", PageType.JUDGEMENT.value)
        .eq("is_superseded", False)
        .order("created_at", desc=True)
        .limit(1)
    )
    rows = verdict_rows.data or []
    if not rows:
        return None
    extra = rows[0].get("extra") or {}
    verdict_data = extra.get("adversarial_verdict")
    if not verdict_data:
        return None
    return AdversarialVerdict(**verdict_data)


async def has_adversarial_review(db: DB, target_page_id: str) -> bool:
    """Return True if target_page_id has a *valid* adversarial review.

    A review counts as valid when (a) a completed ADVERSARIAL_REVIEW call
    exists for the target and (b) its most recent verdict is not expired
    per ``is_verdict_expired``. An expired verdict returns False so the
    gate fires a fresh review.
    """
    rows = await db._execute(
        db.client.table("calls")
        .select("id")
        .eq("call_type", CallType.ADVERSARIAL_REVIEW.value)
        .eq("scope_page_id", target_page_id)
        .eq("status", CallStatus.COMPLETE.value)
        .order("created_at", desc=True)
    )
    call_ids = [r["id"] for r in (rows.data or [])]
    if not call_ids:
        return False

    verdict_rows = await db._execute(
        db.client.table("pages")
        .select("extra")
        .in_("provenance_call_id", call_ids)
        .eq("page_type", PageType.JUDGEMENT.value)
        .eq("is_superseded", False)
        .order("created_at", desc=True)
        .limit(1)
    )
    data = verdict_rows.data or []
    if not data:
        # Call completed but no verdict page materialised — treat as no review
        # so the gate can retry. This matches the "re-review on expiry" intent.
        return False
    extra = data[0].get("extra") or {}
    verdict_data = extra.get("adversarial_verdict")
    if not verdict_data:
        return False
    try:
        verdict = AdversarialVerdict(**verdict_data)
    except Exception:
        log.warning(
            "has_adversarial_review: could not parse verdict for target %s",
            target_page_id[:8],
        )
        return False
    return not is_verdict_expired(verdict)


async def create_view_for_question(
    question_id: str,
    db: DB,
    parent_call_id: str | None = None,
    context_page_ids: Sequence[str] | None = None,
    broadcaster=None,
    force: bool = False,
    call_id: str | None = None,
    sequence_id: str | None = None,
    sequence_position: int | None = None,
) -> str | None:
    """Run a CreateView call on a question. Returns call ID, or None if no budget."""
    from rumil.calls.create_view import CreateViewCall

    log.info("create_view_for_question: question=%s", question_id[:8])
    if not await _consume_budget(db, force=force):
        return None

    call = await db.create_call(
        CallType.CREATE_VIEW,
        scope_page_id=question_id,
        parent_call_id=parent_call_id,
        context_page_ids=context_page_ids,
        call_id=call_id,
        sequence_id=sequence_id,
        sequence_position=sequence_position,
    )
    instance = CreateViewCall(question_id, call, db, broadcaster=broadcaster)
    await instance.run()
    return call.id


async def update_view_for_question(
    question_id: str,
    db: DB,
    parent_call_id: str | None = None,
    context_page_ids: Sequence[str] | None = None,
    broadcaster=None,
    force: bool = False,
    call_id: str | None = None,
    sequence_id: str | None = None,
    sequence_position: int | None = None,
) -> str | None:
    """Run an UpdateView call on a question with an existing View. Returns call ID."""
    from rumil.calls.update_view import UpdateViewCall

    log.info("update_view_for_question: question=%s", question_id[:8])
    if not await _consume_budget(db, force=force):
        return None

    call = await db.create_call(
        CallType.UPDATE_VIEW,
        scope_page_id=question_id,
        parent_call_id=parent_call_id,
        context_page_ids=context_page_ids,
        call_id=call_id,
        sequence_id=sequence_id,
        sequence_position=sequence_position,
    )
    instance = UpdateViewCall(question_id, call, db, broadcaster=broadcaster)
    await instance.run()
    return call.id


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


async def count_sources_for_question(db: DB, question_id: str) -> int:
    """Return the number of Source pages attached to a question (directly or via cites).

    Two discovery paths are checked in one level of BFS:

    * Directly linked from the question (any link type) where the target
      page is a SOURCE. This covers ``cites`` or ``related`` edges placed
      on the question itself.
    * Linked from any active consideration (claim) on the question via a
      ``cites`` edge to a SOURCE. This covers the common shape where
      claims cite source pages.

    Returns the number of unique Source page IDs discovered. Two batched
    DB round trips: one for links, one for pages.
    """
    links_from_q = await db.get_links_from(question_id)
    links_to_q = await db.get_links_to(question_id)
    considerations = await db.get_considerations_for_question(question_id)
    claim_ids = [p.id for p, _ in considerations]

    links_from_claims = await db.get_links_from_many(claim_ids) if claim_ids else {}

    candidate_ids: set[str] = set()
    for link in links_from_q:
        candidate_ids.add(link.to_page_id)
    for link in links_to_q:
        candidate_ids.add(link.from_page_id)
    for claim_links in links_from_claims.values():
        for link in claim_links:
            if link.link_type == LinkType.CITES:
                candidate_ids.add(link.to_page_id)

    if not candidate_ids:
        return 0

    pages = await db.get_pages_by_ids(list(candidate_ids))
    return sum(
        1 for page in pages.values() if page.page_type == PageType.SOURCE and page.is_active()
    )
