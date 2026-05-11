"""Dedupe pipeline for CREATE_QUESTION moves.

Before a scout or other caller creates a new child question, this pipeline
checks whether an existing workspace question could serve the same role. If
a strong enough match is found, the caller links the existing question as a
child of the parent instead of creating a new page.

Pipeline:
    1. Vector search for similar QUESTION pages above a similarity threshold.
    2. Sonnet filter: show the candidates at abstract level, ask which could
       plausibly be a straight swap for the proposed question.
    3. Opus decide: for the highest-similarity survivor, show the parent,
       the proposed child, and the candidate; ask whether the candidate can
       substitute for the proposed child.

Returns the full UUID of the existing question to swap in, or None when
the proposed question should be created normally.
"""

import logging
from collections.abc import Sequence

from pydantic import BaseModel, Field

from rumil.database import DB
from rumil.embeddings import search_pages
from rumil.llm import LLMExchangeMetadata, structured_call
from rumil.models import Call, Page, PageType
from rumil.moves.base import CreatePagePayload
from rumil.settings import get_settings
from rumil.tracing.trace_events import DedupeCandidateItem, QuestionDedupeEvent
from rumil.tracing.tracer import get_trace

log = logging.getLogger(__name__)

SIMILARITY_THRESHOLD = 0.75
OVERFETCH_COUNT = 20
SONNET_FILTER_LIMIT = 10


class _SonnetPicks(BaseModel):
    candidate_ids: list[str] = Field(
        default_factory=list,
        description=(
            "8-char short IDs of candidates that could plausibly be a "
            "straight swap for the proposed question. Empty list if none "
            "qualify."
        ),
    )


class _OpusDecision(BaseModel):
    swap: bool = Field(
        description=(
            "True if the existing candidate can substitute for the proposed "
            "child: linking the candidate as a child of the parent would "
            "serve the same research purpose as creating the proposed new "
            "question. False otherwise."
        )
    )
    reasoning: str = Field(
        default="",
        description="One or two sentences explaining the decision.",
    )


async def try_dedupe_question_swap(
    payload: CreatePagePayload,
    parent_id: str,
    call: Call,
    db: DB,
) -> str | None:
    """Check whether an existing question should substitute for *payload*.

    Returns the full UUID of an existing question that can substitute, or
    None if the proposed question should be created normally.
    """
    if payload.supersedes:
        return None

    parent = await db.get_page(parent_id)
    parent_headline = parent.headline if parent else ""

    query_text = f"{payload.headline}\n\n{payload.content}"
    try:
        raw_matches = await search_pages(
            db,
            query_text,
            match_threshold=SIMILARITY_THRESHOLD,
            match_count=OVERFETCH_COUNT,
            field_name="abstract",
            input_type="document",
        )
    except Exception:
        log.warning(
            "Question dedupe: vector search failed; proceeding with create",
            exc_info=True,
        )
        await _record_dedupe_event(
            payload=payload,
            parent_id=parent_id,
            parent_headline=parent_headline,
            candidates_with_kept=[],
            outcome="error",
            matched_page=None,
            decision_reasoning="",
        )
        return None

    excluded_ids = {parent_id}
    if call.scope_page_id:
        excluded_ids.add(call.scope_page_id)

    candidates: list[tuple[Page, float]] = [
        (p, score)
        for p, score in raw_matches
        if p.page_type == PageType.QUESTION and p.is_active() and p.id not in excluded_ids
    ]
    if not candidates:
        await _record_dedupe_event(
            payload=payload,
            parent_id=parent_id,
            parent_headline=parent_headline,
            candidates_with_kept=[],
            outcome="no_candidates",
            matched_page=None,
            decision_reasoning="",
        )
        return None

    candidates = candidates[:SONNET_FILTER_LIMIT]

    picks = await _sonnet_filter(payload, candidates, call, db)
    candidates_with_kept: list[tuple[Page, float, bool]] = [
        (p, score, p.id[:8] in picks) for p, score in candidates
    ]
    survivors = [(p, score) for p, score, kept in candidates_with_kept if kept]
    if not survivors:
        await _record_dedupe_event(
            payload=payload,
            parent_id=parent_id,
            parent_headline=parent_headline,
            candidates_with_kept=candidates_with_kept,
            outcome="filter_rejected_all",
            matched_page=None,
            decision_reasoning="",
        )
        return None

    top_candidate, top_score = survivors[0]
    if not parent:
        log.warning(
            "Question dedupe: parent %s not found; proceeding with create",
            parent_id[:8],
        )
        await _record_dedupe_event(
            payload=payload,
            parent_id=parent_id,
            parent_headline=parent_headline,
            candidates_with_kept=candidates_with_kept,
            outcome="error",
            matched_page=None,
            decision_reasoning="",
        )
        return None

    decision = await _opus_decide(payload, parent, top_candidate, call, db)
    decision_reasoning = decision.reasoning if decision else ""
    if decision is None or not decision.swap:
        await _record_dedupe_event(
            payload=payload,
            parent_id=parent_id,
            parent_headline=parent_headline,
            candidates_with_kept=candidates_with_kept,
            outcome="opus_rejected" if decision is not None else "error",
            matched_page=None,
            decision_reasoning=decision_reasoning,
        )
        return None

    log.info(
        "Question dedupe: swapping proposed %r for existing %s (similarity %.2f)",
        payload.headline[:60],
        top_candidate.id[:8],
        top_score,
    )
    await _record_dedupe_event(
        payload=payload,
        parent_id=parent_id,
        parent_headline=parent_headline,
        candidates_with_kept=candidates_with_kept,
        outcome="swap",
        matched_page=top_candidate,
        decision_reasoning=decision_reasoning,
    )
    return top_candidate.id


async def _record_dedupe_event(
    payload: CreatePagePayload,
    parent_id: str,
    parent_headline: str,
    candidates_with_kept: Sequence[tuple[Page, float, bool]],
    outcome: str,
    matched_page: Page | None,
    decision_reasoning: str,
) -> None:
    trace = get_trace()
    if trace is None:
        return
    candidate_items = [
        DedupeCandidateItem(
            id=p.id,
            headline=p.headline,
            similarity=score,
            kept_by_filter=kept,
        )
        for p, score, kept in candidates_with_kept
    ]
    await trace.record(
        QuestionDedupeEvent(
            proposed_headline=payload.headline,
            parent_id=parent_id,
            parent_headline=parent_headline,
            candidates=candidate_items,
            outcome=outcome,
            matched_page_id=matched_page.id if matched_page else None,
            matched_headline=matched_page.headline if matched_page else "",
            decision_reasoning=decision_reasoning,
        )
    )


async def _sonnet_filter(
    payload: CreatePagePayload,
    candidates: Sequence[tuple[Page, float]],
    call: Call,
    db: DB,
) -> set[str]:
    """Ask Sonnet which candidates could plausibly substitute for the proposed question."""
    proposed_block = f"### Proposed question\n\n**{payload.headline}**\n\n{payload.content}"
    candidate_blocks: list[str] = []
    for page, score in candidates:
        if page.abstract and page.abstract.strip():
            body = page.abstract.strip()
        else:
            body = "(no abstract — page has not been through closing review yet)"
        candidate_blocks.append(
            f"### `{page.id[:8]}` (similarity {score:.2f})\n\n**{page.headline}**\n\n{body}"
        )
    user_message = (
        f"{proposed_block}\n\n---\n\n"
        f"## Candidate existing questions ({len(candidates)})\n\n"
        + "\n\n---\n\n".join(candidate_blocks)
        + "\n\n---\n\nReturn the 8-char short IDs of candidates that could "
        "plausibly be a straight swap for the proposed question — i.e. "
        "candidates whose answer would serve the same research purpose. "
        "Return an empty list if none qualify."
    )
    system_prompt = (
        "You are filtering candidate duplicate questions. A new research "
        "question has been proposed, and a vector search has surfaced "
        "existing questions with similar headlines. Your job is to select "
        "the candidates that could plausibly substitute for the proposed "
        "question — i.e. where linking the existing question would serve "
        "the same research purpose as creating the new one.\n\n"
        "Be generous at this filtering stage — a stronger model will make "
        "the final decision on each survivor — but exclude candidates that "
        "clearly ask a different question, address a different subject, or "
        "would not substitute for the proposed question."
    )
    settings = get_settings()
    try:
        result = await structured_call(
            system_prompt=system_prompt,
            user_message=user_message,
            response_model=_SonnetPicks,
            metadata=LLMExchangeMetadata(
                call_id=call.id,
                phase="dedupe_question_sonnet_filter",
            ),
            db=db,
            model=settings.sonnet_model,
        )
    except Exception:
        log.warning("Question dedupe: sonnet filter failed", exc_info=True)
        return set()
    if result.parsed is None:
        return set()
    return {sid for sid in result.parsed.candidate_ids if isinstance(sid, str)}


async def _opus_decide(
    payload: CreatePagePayload,
    parent: Page,
    candidate: Page,
    call: Call,
    db: DB,
) -> _OpusDecision | None:
    """Ask Opus whether the candidate can substitute for the proposed question."""
    user_message = (
        f"## Parent question\n\n**{parent.headline}**\n\n{parent.content}\n\n"
        "---\n\n"
        f"## Proposed new child question\n\n**{payload.headline}**\n\n{payload.content}\n\n"
        "---\n\n"
        f"## Existing candidate question `{candidate.id[:8]}`\n\n"
        f"**{candidate.headline}**\n\n{candidate.content}\n\n"
        "---\n\n"
        "Can the existing candidate be linked as a child of the parent "
        "question INSTEAD of creating the proposed new child? Answer yes "
        "only if the candidate's answer would serve the same research "
        "purpose for the parent as the proposed new question's answer "
        "would. A yes means: we link the existing candidate to the parent "
        "and skip creating a new page."
    )
    system_prompt = (
        "You are deciding whether to reuse an existing research question "
        "instead of creating a new one. A scout has proposed adding a new "
        "child question to a parent question. A similar existing question "
        "has been surfaced as a candidate substitute. Your job is to decide "
        "whether linking the existing candidate as a child of the parent "
        "would serve the same research purpose as creating the proposed "
        "new question.\n\n"
        "Apply a strict bar. Say yes only when the candidate is a true "
        "substitute in the present context: answering the candidate would "
        "answer the proposal in full, with no substantive piece of the "
        "proposal left unaddressed.\n\n"
        "Reasons to say no — any one is sufficient:\n"
        "  - The proposal contains substantive material the candidate does "
        "not cover: an extra clause, conjunct, qualifier, mechanism, "
        "comparison, or downstream implication. A candidate that answers "
        "only part of the proposal is not a substitute, even if the "
        "uncovered part is short.\n"
        "  - The two questions carry different presuppositions or treat "
        "different things as open. A question that assumes X and asks "
        "about its consequences is not a substitute for one that asks "
        "whether or to what extent X holds (and vice versa).\n"
        "  - The candidate is narrower, broader, or differently scoped "
        "from the proposal in ways that would change what counts as an "
        "answer or what evidence would bear on it.\n"
        "  - The proposal ties its topic to something specific in the "
        "parent (a particular mechanism, actor, condition, or framing) "
        "that the candidate does not engage with.\n\n"
        "Topical overlap, shared keywords, and pointing at the same general "
        "phenomenon are not enough. Reusing a question that almost matches "
        "is worse than creating a new one, because it silently drops the "
        "parts that did not match. The default is no — only answer yes "
        "when substitution is unambiguous."
    )
    settings = get_settings()
    try:
        result = await structured_call(
            system_prompt=system_prompt,
            user_message=user_message,
            response_model=_OpusDecision,
            metadata=LLMExchangeMetadata(
                call_id=call.id,
                phase="dedupe_question_opus_decide",
            ),
            db=db,
            model=settings.model,
        )
    except Exception:
        log.warning("Question dedupe: opus decide failed", exc_info=True)
        return None
    if result.parsed is None:
        return None
    log.info(
        "Question dedupe opus decision: swap=%s reasoning=%s",
        result.parsed.swap,
        result.parsed.reasoning[:200] if result.parsed.reasoning else "",
    )
    return result.parsed
