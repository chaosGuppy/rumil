"""ClosingReviewer implementations for all call types."""

from __future__ import annotations

import logging
from collections.abc import Sequence

from rumil.available_moves import get_moves_for_call
from rumil.calls.common import (
    PageSummaryItem,
    ReviewResponse,
    format_moves_for_review,
    log_page_ratings,
    mark_call_completed,
    prepare_tools,
    run_closing_review,
    save_page_abstracts,
)
from rumil.calls.stages import CallInfra, ClosingReviewer, ContextResult, UpdateResult
from rumil.context import format_page
from rumil.llm import (
    LLMExchangeMetadata,
    build_system_prompt,
    structured_call,
    text_call,
)
from rumil.models import CallType, MoveType, PageDetail, PageType
from rumil.moves.load_page import LoadPagePayload
from rumil.moves.registry import MOVES
from rumil.tracing.trace_events import ReviewCompleteEvent

log = logging.getLogger(__name__)


class StandardClosingReview(ClosingReviewer):
    """Standard closing review using run_closing_review(). Used by most call types."""

    def __init__(self, call_type: CallType) -> None:
        self._call_type = call_type

    async def closing_review(
        self,
        infra: CallInfra,
        context: ContextResult,
        creation: UpdateResult,
    ) -> None:
        review_context = format_moves_for_review(creation.moves)
        review = await run_closing_review(
            infra.call,
            review_context,
            context.context_text,
            creation.all_loaded_ids,
            creation.created_page_ids,
            infra.db,
        )
        if review:
            log.info(
                "%s review: confidence=%s",
                self._call_type.value.capitalize(),
                review.get("confidence_in_output", "?"),
            )
            await log_page_ratings(review, infra.db)
            await infra.trace.record_strict(
                ReviewCompleteEvent(
                    remaining_fruit=review.get("remaining_fruit"),
                    confidence=review.get("confidence_in_output"),
                )
            )
        infra.call.review_json = review or {}

        summary = self._result_summary(creation)
        await mark_call_completed(infra.call, infra.db, summary)

    def _result_summary(self, creation: UpdateResult) -> str:
        return (
            f"{self._call_type.value.capitalize()} complete. "
            f"Created {len(creation.created_page_ids)} pages."
        )


class ViewClosingReview(StandardClosingReview):
    """Closing review for View creation: generates the NL summary for importance-5 items."""

    def __init__(self, call_type: CallType, view_id: str) -> None:
        super().__init__(call_type)
        self._view_id = view_id

    async def closing_review(
        self,
        infra: CallInfra,
        context: ContextResult,
        creation: UpdateResult,
    ) -> None:
        items = await infra.db.get_view_items(self._view_id, min_importance=4)
        if items:
            item_lines: list[str] = []
            for page, link in items:
                imp = link.importance or 0
                marker = " [IMPORTANCE 5 — FOCUS]" if imp == 5 else ""
                formatted = await format_page(
                    page,
                    PageDetail.CONTENT,
                    linked_detail=None,
                    db=infra.db,
                    track=True,
                    track_tags={"source": "closing_review_view"},
                )
                item_lines.append(
                    f"### [R{page.robustness} I{imp}]{marker} {page.headline}\n\n{formatted}\n"
                )
            items_text = "\n".join(item_lines)

            question = await infra.db.get_page(infra.question_id)
            q_headline = question.headline if question else "the question"

            summary = await text_call(
                system_prompt=(
                    "You are writing a concise natural-language summary for a View page. "
                    "The View summarizes current understanding on a research question. "
                    "Focus especially on the IMPORTANCE 5 items — these are the most "
                    "critical things to know. Frame their interactions, tensions, and "
                    "overall epistemic posture. The summary should orient a reader who "
                    "will see the individual items listed below it, so focus on synthesis "
                    "and framing rather than repeating items verbatim. "
                    "Keep it to 2-4 paragraphs."
                ),
                user_message=(
                    f"Question: {q_headline}\n\n"
                    f"## View Items (importance 4+)\n\n{items_text}\n\n"
                    "Write the NL summary for this View."
                ),
            )

            await infra.db.update_page_content(self._view_id, summary)
            log.info(
                "View %s NL summary written (%d chars)",
                self._view_id[:8],
                len(summary),
            )

        await super().closing_review(infra, context, creation)

    def _result_summary(self, creation: UpdateResult) -> str:
        return f"View created. {len(creation.created_page_ids)} items."


class IngestClosingReview(StandardClosingReview):
    """Closing review that includes the source filename in the summary."""

    def __init__(self, call_type: CallType, filename: str) -> None:
        super().__init__(call_type)
        self._filename = filename

    def _result_summary(self, creation: UpdateResult) -> str:
        return (
            f"Ingest complete. Created {len(creation.created_page_ids)} "
            f"pages from '{self._filename}'."
        )


class WebResearchClosingReview(StandardClosingReview):
    """Closing review for web research that includes source count in summary."""

    def __init__(self, call_type: CallType, page_creator=None) -> None:
        super().__init__(call_type)
        self._page_creator = page_creator

    def _result_summary(self, creation: UpdateResult) -> str:
        source_count = (
            len(self._page_creator.source_page_ids) if self._page_creator is not None else 0
        )
        return (
            f"Web research complete. {len(creation.created_page_ids)} claims created, "
            f"{source_count} sources cited."
        )


_SELF_ASSESSMENT_INSTRUCTION = (
    "Now provide your self-assessment. Do not call any tools \u2014 they will "
    "have no effect here.\n\n"
    "Scope question ID: `{question_id}`"
)


async def _self_assessment(
    infra: CallInfra,
    system_prompt: str,
    tool_defs: Sequence[dict],
    prior_messages: Sequence[dict],
    loaded_summaries: Sequence[tuple[str, str]],
) -> dict:
    """Structured self-assessment appended to a message history."""

    page_rating_note = ""
    if loaded_summaries:
        page_lines = [f'  - `{pid[:8]}`: "{summary[:120]}"' for pid, summary in loaded_summaries]
        page_rating_note = (
            "\n\nThe following pages were loaded into your context:\n"
            + "\n".join(page_lines)
            + "\n\nPlease include a rating for each in your page_ratings. "
            "Scores: -1 = actively confusing, 0 = didn't help, "
            "1 = helped, 2 = extremely helpful."
        )

    page_summary_note = ""
    if infra.state.created_page_ids:
        created_lines = []
        for pid in infra.state.created_page_ids:
            page = await infra.db.get_page(pid)
            if page and page.page_type != PageType.SOURCE:
                created_lines.append(f'  - `{pid[:8]}`: "{page.headline[:120]}"')
        if created_lines:
            page_summary_note = (
                "\n\nYou created the following pages during this call:\n"
                + "\n".join(created_lines)
                + "\n\nFor each, provide an abstract (~200 words, fully self-contained) "
                "in your page_summaries. "
                "These will be read by other LLM instances with no prior context, so do not "
                "assume any background knowledge."
            )

    assessment_msg = (
        _SELF_ASSESSMENT_INSTRUCTION.format(question_id=infra.question_id)
        + page_rating_note
        + page_summary_note
    )
    assessment_messages = [*prior_messages, {"role": "user", "content": assessment_msg}]
    meta = LLMExchangeMetadata(
        call_id=infra.call.id,
        phase="closing_review",
        user_message=assessment_msg,
    )
    review_result = await structured_call(
        system_prompt=system_prompt,
        response_model=ReviewResponse,
        messages=assessment_messages,
        tools=tool_defs,
        metadata=meta,
        db=infra.db,
        cache=True,
    )
    review_data = review_result.parsed.model_dump() if review_result.parsed else {}

    if review_data:
        log.info(
            "Scout session review: confidence=%s",
            review_data.get("confidence_in_output", "?"),
        )
        await log_page_ratings(review_data, infra.db)

        for r in review_data.get("page_ratings", []):
            pid = await infra.db.resolve_page_id(r.get("page_id", ""))
            score = r.get("score")
            if pid and isinstance(score, int):
                await infra.db.save_page_rating(
                    pid,
                    infra.call.id,
                    score,
                    r.get("note", ""),
                )
        raw_summaries = review_data.get("page_summaries", [])
        items = [
            PageSummaryItem(**s) for s in raw_summaries if isinstance(s, dict) and s.get("page_id")
        ]
        await save_page_abstracts(items, infra.db)

    return review_data


async def _collect_all_loaded_summaries(
    infra: CallInfra,
    preloaded_ids: Sequence[str],
) -> list[tuple[str, str]]:

    summaries: list[tuple[str, str]] = []
    seen: set[str] = set()

    for m in infra.state.moves:
        if m.move_type == MoveType.LOAD_PAGE:
            assert isinstance(m.payload, LoadPagePayload)
            full_id = await infra.db.resolve_page_id(m.payload.page_id)
            if full_id and full_id not in seen:
                page = await infra.db.get_page(full_id)
                if page:
                    summaries.append((full_id, page.headline))
                    seen.add(full_id)

    for pid in preloaded_ids:
        if pid not in seen:
            page = await infra.db.get_page(pid)
            if page:
                summaries.append((pid, page.headline))
                seen.add(pid)

    return summaries


class SinglePhaseScoutReview(ClosingReviewer):
    """Closing review for the scout call: self-assessment only."""

    async def closing_review(
        self,
        infra: CallInfra,
        context: ContextResult,
        creation: UpdateResult,
    ) -> None:
        if not creation.messages:
            infra.call.review_json = {}
            summary = (
                f"Scout session complete. {creation.rounds_completed} rounds, "
                f"{len(creation.created_page_ids)} pages created."
            )
            await mark_call_completed(infra.call, infra.db, summary)
            return

        assert creation.last_fruit_score is not None
        loaded_summaries = await _collect_all_loaded_summaries(
            infra,
            context.preloaded_ids,
        )

        move_types = get_moves_for_call(infra.call.call_type)
        tools = [MOVES[mt].bind(infra.state) for mt in move_types]
        tool_defs, _ = prepare_tools(tools)
        system_prompt = build_system_prompt(infra.call.call_type.value)

        review_data = await _self_assessment(
            infra,
            system_prompt,
            tool_defs,
            list(creation.messages),
            loaded_summaries,
        )

        infra.call.review_json = review_data
        await infra.trace.record_strict(
            ReviewCompleteEvent(
                remaining_fruit=creation.last_fruit_score,
                confidence=review_data.get("confidence_in_output"),
            )
        )

        summary = (
            f"Scout session complete. {creation.rounds_completed} rounds, "
            f"{len(creation.created_page_ids)} pages created."
        )
        await mark_call_completed(infra.call, infra.db, summary)
