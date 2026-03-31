"""ClosingReviewer implementations for all call types."""

from __future__ import annotations

import logging
from collections.abc import Sequence

from rumil.calls.assess_concept_types import (
    ConceptAssessmentReview,
    REVIEW_SYSTEM_PROMPT,
    VALIDATION_PHASE,
)
from rumil.calls.common import (
    PageSummaryItem,
    ReviewResponse,
    format_moves_for_review,
    log_page_ratings,
    mark_call_completed,
    prepare_tools,
    run_closing_review,
    run_single_call,
    save_page_abstracts,
)
from rumil.calls.stages import CallInfra, ClosingReviewer, ContextResult, CreationResult
from rumil.llm import (
    LLMExchangeMetadata,
    build_system_prompt,
    build_user_message,
    structured_call,
)
from rumil.models import CallType, MoveType, PageType
from rumil.available_moves import get_moves_for_call
from rumil.moves.load_page import LoadPagePayload
from rumil.moves.registry import MOVES
from rumil.tracing.trace_events import ErrorEvent, ReviewCompleteEvent
from rumil.tracing.tracer import get_trace

log = logging.getLogger(__name__)


class StandardClosingReview(ClosingReviewer):
    """Standard closing review using run_closing_review(). Used by most call types."""

    def __init__(self, call_type: CallType) -> None:
        self._call_type = call_type

    async def closing_review(
        self,
        infra: CallInfra,
        context: ContextResult,
        creation: CreationResult,
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
            await infra.trace.record(
                ReviewCompleteEvent(
                    remaining_fruit=review.get("remaining_fruit"),
                    confidence=review.get("confidence_in_output"),
                )
            )
        infra.call.review_json = review or {}

        summary = self._result_summary(creation)
        await mark_call_completed(infra.call, infra.db, summary)

    def _result_summary(self, creation: CreationResult) -> str:
        return (
            f"{self._call_type.value.capitalize()} complete. "
            f"Created {len(creation.created_page_ids)} pages."
        )


class IngestClosingReview(StandardClosingReview):
    """Closing review that includes the source filename in the summary."""

    def __init__(self, call_type: CallType, filename: str) -> None:
        super().__init__(call_type)
        self._filename = filename

    def _result_summary(self, creation: CreationResult) -> str:
        return (
            f"Ingest complete. Created {len(creation.created_page_ids)} "
            f"pages from '{self._filename}'."
        )


class WebResearchClosingReview(StandardClosingReview):
    """Closing review for web research that includes source count in summary."""

    def __init__(self, call_type: CallType, page_creator=None) -> None:
        super().__init__(call_type)
        self._page_creator = page_creator

    def _result_summary(self, creation: CreationResult) -> str:
        source_count = (
            len(self._page_creator.source_page_ids)
            if self._page_creator is not None
            else 0
        )
        return (
            f"Web research complete. {len(creation.created_page_ids)} claims created, "
            f"{source_count} sources cited."
        )


_LINK_REVIEW_INSTRUCTION = (
    "You have finished scouting. Before your self-assessment, review the "
    "links on the scope question.\n\n"
    "For each link below, decide whether it should stay as-is, have its "
    "role changed (direct \u2194 structural), or be removed entirely.\n\n"
    "- **direct**: the linked page directly bears on the answer.\n"
    "- **structural**: the linked page frames what evidence/angles to explore.\n"
    "- **remove**: the link is no longer relevant or useful.\n\n"
    "Use `change_link_role` to switch a link between direct and structural. "
    "Use `remove_link` to delete a link that should not exist. "
    "Leave links alone if they are already correct.\n\n"
    "{link_inventory}\n\n"
    "Scope question ID: `{question_id}`"
)

_SELF_ASSESSMENT_INSTRUCTION = (
    "Now provide your self-assessment. Do not call any tools \u2014 they will "
    "have no effect here.\n\n"
    "Scope question ID: `{question_id}`"
)


async def _build_link_inventory(
    question_id: str,
    db,
    graph=None,
) -> str:
    source = graph if graph is not None else db
    considerations = await source.get_considerations_for_question(question_id)
    children_with_links = await source.get_child_questions_with_links(question_id)

    if not considerations and not children_with_links:
        return "No existing links on the scope question."

    lines = ["### Current Links"]
    for page, link in considerations:
        lines.append(
            f"- [{link.role.value}] consideration: "
            f'"{page.headline}" '
            f"(strength {link.strength:.1f}, link_id: `{link.id}`)"
        )
    for page, link in children_with_links:
        lines.append(
            f"- [{link.role.value}] child_question: "
            f'"{page.headline}" '
            f"(link_id: `{link.id}`)"
        )
    return "\n".join(lines)


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
        page_lines = [
            f'  - `{pid[:8]}`: "{summary[:120]}"' for pid, summary in loaded_summaries
        ]
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
    review_data = review_result.data or {}

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
            PageSummaryItem(**s)
            for s in raw_summaries
            if isinstance(s, dict) and s.get("page_id")
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


class TwoPhaseScoutReview(ClosingReviewer):
    """Two-phase closing: link modification then self-assessment. Used by FindConsiderationsCall."""

    async def closing_review(
        self,
        infra: CallInfra,
        context: ContextResult,
        creation: CreationResult,
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

        link_inventory = await _build_link_inventory(infra.question_id, infra.db)
        link_review_msg = _LINK_REVIEW_INSTRUCTION.format(
            link_inventory=link_inventory,
            question_id=infra.question_id,
        )
        link_messages = list(creation.messages) + [
            {"role": "user", "content": link_review_msg},
        ]

        link_tools = [MOVES[mt].bind(infra.state) for mt in move_types]
        link_result = await run_single_call(
            system_prompt,
            tools=link_tools,
            call_id=infra.call.id,
            phase="link_review",
            db=infra.db,
            state=infra.state,
            messages=link_messages,
            cache=True,
        )
        post_link_messages = list(link_result.messages)

        review_data = await _self_assessment(
            infra,
            system_prompt,
            tool_defs,
            post_link_messages,
            loaded_summaries,
        )

        infra.call.review_json = review_data
        await infra.trace.record(
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


class SinglePhaseScoutReview(ClosingReviewer):
    """Skip link review, go straight to self-assessment. Used by EmbeddingFindConsiderationsCall."""

    async def closing_review(
        self,
        infra: CallInfra,
        context: ContextResult,
        creation: CreationResult,
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
        await infra.trace.record(
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


class ConceptAssessReview(ClosingReviewer):
    """Concept assessment closing review. Used by AssessConceptCall."""

    def __init__(self, phase: str) -> None:
        self._phase = phase
        self.concept_assessment: dict = {}

    async def closing_review(
        self,
        infra: CallInfra,
        context: ContextResult,
        creation: CreationResult,
    ) -> None:
        review_context = format_moves_for_review(creation.moves)
        review_task = (
            f"You have just completed an assess_concept call ({self._phase} phase).\n\n"
            f"Here is your output from that call:\n{review_context}\n\n"
            "Please review your assessment and provide structured feedback."
        )

        user_message = build_user_message(context.context_text, review_task)
        meta = LLMExchangeMetadata(
            call_id=infra.call.id,
            phase="closing_review",
            user_message=user_message,
        )
        try:
            result = await structured_call(
                system_prompt=REVIEW_SYSTEM_PROMPT,
                user_message=user_message,
                response_model=ConceptAssessmentReview,
                metadata=meta,
                db=infra.db,
            )
            review = result.data
        except Exception as e:
            log.error(
                "Concept closing review failed for call=%s: %s",
                infra.call.id[:8],
                e,
                exc_info=True,
            )
            trace = get_trace()
            if trace:
                await trace.record(
                    ErrorEvent(
                        message=f"Concept closing review failed: {e}",
                        phase="closing_review",
                    )
                )
            infra.call.review_json = {}
            await mark_call_completed(
                infra.call,
                infra.db,
                f"Assess concept complete ({self._phase}). Review failed.",
            )
            return

        if not review:
            infra.call.review_json = {}
            await mark_call_completed(
                infra.call,
                infra.db,
                f"Assess concept complete ({self._phase}). No review.",
            )
            return

        log.info(
            "Concept review (%s): score=%s, screening_passed=%s, fruit=%s",
            self._phase,
            review.get("score"),
            review.get("screening_passed"),
            review.get("remaining_fruit"),
        )

        self.concept_assessment = review
        infra.call.review_json = review

        await infra.trace.record(
            ReviewCompleteEvent(
                remaining_fruit=review.get("remaining_fruit"),
                confidence=review.get("confidence_in_output"),
            )
        )

        await self._persist_assessment_round(infra, review)

        score = review.get("score")
        screening = review.get("screening_passed")
        summary = (
            f"Assess concept complete ({self._phase}). "
            f"score={score}, screening_passed={screening}."
        )
        await mark_call_completed(infra.call, infra.db, summary)

    async def _persist_assessment_round(
        self,
        infra: CallInfra,
        review: dict,
    ) -> None:
        concept = await infra.db.get_page(infra.question_id)
        if not concept:
            return
        extra = dict(concept.extra or {})
        rounds = list(extra.get("assessment_rounds", []))
        rounds.append(
            {
                "phase": self._phase,
                "call_id": infra.call.id,
                "score": review.get("score"),
                "remaining_fruit": review.get("remaining_fruit"),
                "screening_passed": review.get("screening_passed"),
                "what_worked": review.get("what_worked", ""),
                "what_didnt": review.get("what_didnt", ""),
                "could_existing_claims_be_restated": review.get(
                    "could_existing_claims_be_restated"
                ),
                "did_it_reveal_new_considerations": review.get(
                    "did_it_reveal_new_considerations"
                ),
                "did_it_resolve_existing_tensions": review.get(
                    "did_it_resolve_existing_tensions"
                ),
                "suggested_refinements": review.get("suggested_refinements", ""),
            }
        )
        extra["assessment_rounds"] = rounds
        extra["score"] = review.get("score")
        extra["screening_passed"] = review.get("screening_passed")
        extra["stage"] = "validated" if self._phase == VALIDATION_PHASE else "screened"
        await infra.db.update_page_extra(infra.question_id, extra)
