"""Assess Concept call: evaluate a staged concept proposal."""

import logging

from pydantic import BaseModel, Field

from rumil.calls.base import SimpleCall
from rumil.calls.common import (
    format_moves_for_review,
    resolve_page_refs,
)
from rumil.context import format_page
from rumil.database import DB
from rumil.llm import build_user_message, structured_call, LLMExchangeMetadata
from rumil.models import Call, CallStage, CallType, MoveType, PageDetail
from rumil.tracing.trace_events import ContextBuiltEvent, ReviewCompleteEvent
from rumil.workspace_map import build_workspace_map

log = logging.getLogger(__name__)

SCREENING_PHASE = "screening"
VALIDATION_PHASE = "validation"

SCREENING_MAX_ROUNDS = 2
VALIDATION_MAX_ROUNDS = 8
SCREENING_FRUIT_THRESHOLD = 5
VALIDATION_FRUIT_THRESHOLD = 3


class ConceptAssessmentReview(BaseModel):
    remaining_fruit: int = Field(
        description=(
            "0-10: how much more testing would meaningfully change your assessment. "
            "0 = verdict is settled; 10 = barely started."
        )
    )
    confidence_in_output: float = Field(description="0-5 confidence in this assessment")
    score: int = Field(
        description=(
            "1-10: overall usefulness of this concept for the research. "
            "1-4 = not useful enough to warrant promotion; "
            "5-7 = moderately useful, borderline; "
            "8-10 = clearly useful, strong candidate for promotion."
        )
    )
    what_worked: str = Field(description="Where the concept added clarity or revealed something")
    what_didnt: str = Field(description="Where the concept failed or added noise")
    could_existing_claims_be_restated: bool = Field(
        description="Whether existing claims could be stated more usefully with this concept"
    )
    did_it_reveal_new_considerations: bool = Field(
        description="Whether applying the concept surfaced considerations not already in the workspace"
    )
    did_it_resolve_existing_tensions: bool = Field(
        description="Whether the concept dissolved any apparent contradictions or tensions"
    )
    suggested_refinements: str = Field(
        "", description="How the concept could be sharpened or narrowed to be more useful"
    )
    screening_passed: bool = Field(
        description=(
            "Whether this concept warrants deeper validation. "
            "True if there is genuine promise, even if uncertain. "
            "False if the concept clearly does not add value."
        )
    )


REVIEW_SYSTEM_PROMPT = (
    "You are a research assistant completing a closing review of a concept assessment "
    "you just performed. Be honest and specific. Most concept proposals should not be "
    "promoted — a clear 'no' is more useful than an uncertain 'maybe'."
)


class AssessConceptCall(SimpleCall):
    """Assess a staged concept proposal: one round of testing."""

    def __init__(
        self,
        concept_page_id: str,
        call: Call,
        db: DB,
        *,
        phase: str = SCREENING_PHASE,
        broadcaster=None,
        up_to_stage: CallStage | None = None,
    ):
        super().__init__(
            concept_page_id, call, db,
            broadcaster=broadcaster, up_to_stage=up_to_stage,
        )
        self.phase = phase
        self.concept_assessment: dict = {}

    def call_type(self) -> CallType:
        return CallType.ASSESS_CONCEPT

    def task_description(self) -> str:
        phase_note = (
            "This is the **screening phase** — form a quick initial verdict. "
            "Load a few representative pages and test the concept against them. "
            "Set `screening_passed` based on whether you see genuine potential."
            if self.phase == SCREENING_PHASE
            else
            "This is the **validation phase** — do thorough testing. "
            "Load more pages, try to restate claims through the concept lens, "
            "look for edge cases. "
            "Call `promote_concept` if and only if you are confident it earns its place."
        )
        return (
            f"Assess the concept proposal above.\n\n{phase_note}\n\n"
            f"Concept page ID: `{self.question_id}`"
        )

    def result_summary(self) -> str:
        score = self.concept_assessment.get("score")
        screening = self.concept_assessment.get("screening_passed")
        return (
            f"Assess concept complete ({self.phase}). "
            f"score={score}, screening_passed={screening}."
        )

    async def build_context(self) -> None:
        concept = await self.db.get_page(self.question_id)
        if not concept:
            self.context_text = f"[Concept page {self.question_id} not found]"
            return

        map_text, _ = await build_workspace_map(self.db)
        concept_text = await format_page(concept, PageDetail.HEADLINE, db=self.db)

        extra = concept.extra or {}
        assessment_rounds = extra.get("assessment_rounds", [])
        history_section = ""
        if assessment_rounds:
            lines = ["## Previous Assessment Rounds", ""]
            for i, r in enumerate(assessment_rounds):
                lines.append(
                    f"Round {i + 1} ({r.get('phase', '?')}): "
                    f"score={r.get('score', '?')}, "
                    f"remaining_fruit={r.get('remaining_fruit', '?')}"
                )
                if r.get("what_worked"):
                    lines.append(f"  Worked: {r['what_worked']}")
                if r.get("what_didnt"):
                    lines.append(f"  Didn't: {r['what_didnt']}")
            history_section = "\n\n" + "\n".join(lines)

        self.context_text = "\n\n".join([
            map_text,
            "---",
            "## Concept Under Assessment",
            "",
            concept_text,
        ]) + history_section

        self.working_page_ids = [self.question_id]
        await self.trace.record(ContextBuiltEvent(
            working_context_page_ids=await resolve_page_refs(
                self.working_page_ids, self.db,
            ),
            preloaded_page_ids=await resolve_page_refs(self.preloaded_ids, self.db),
        ))
        await self._load_phase1_pages()

    async def create_pages(self) -> None:
        if self.phase == VALIDATION_PHASE:
            self.available_moves = [MoveType.PROMOTE_CONCEPT, MoveType.LOAD_PAGE]
        else:
            self.available_moves = [MoveType.LOAD_PAGE]
        await super().create_pages()

    async def closing_review(self) -> None:
        review_context = format_moves_for_review(self.result.moves)
        review_task = (
            f"You have just completed an assess_concept call ({self.phase} phase).\n\n"
            f"Here is your output from that call:\n{review_context}\n\n"
            "Please review your assessment and provide structured feedback."
        )

        user_message = build_user_message(self.context_text, review_task)
        meta = LLMExchangeMetadata(
            call_id=self.call.id, phase="closing_review",
            trace=self.trace, user_message=user_message,
        )
        try:
            result = await structured_call(
                system_prompt=REVIEW_SYSTEM_PROMPT,
                user_message=user_message,
                response_model=ConceptAssessmentReview,
                max_tokens=2048,
                metadata=meta,
                db=self.db,
            )
            review = result.data
        except Exception as e:
            log.error(
                "Concept closing review failed for call=%s: %s",
                self.call.id[:8], e, exc_info=True,
            )
            self.review = {}
            return

        if not review:
            self.review = {}
            return

        log.info(
            "Concept review (%s): score=%s, screening_passed=%s, fruit=%s",
            self.phase,
            review.get("score"),
            review.get("screening_passed"),
            review.get("remaining_fruit"),
        )

        self.concept_assessment = review
        self.review = review

        await self.trace.record(ReviewCompleteEvent(
            remaining_fruit=review.get("remaining_fruit"),
            confidence=review.get("confidence_in_output"),
        ))

        await self._persist_assessment_round(review)

    async def _persist_assessment_round(self, review: dict) -> None:
        """Append this round's assessment to the concept page's extra.assessment_rounds."""
        concept = await self.db.get_page(self.question_id)
        if not concept:
            return
        extra = dict(concept.extra or {})
        rounds = list(extra.get("assessment_rounds", []))
        rounds.append({
            "phase": self.phase,
            "call_id": self.call.id,
            "score": review.get("score"),
            "remaining_fruit": review.get("remaining_fruit"),
            "screening_passed": review.get("screening_passed"),
            "what_worked": review.get("what_worked", ""),
            "what_didnt": review.get("what_didnt", ""),
            "could_existing_claims_be_restated": review.get("could_existing_claims_be_restated"),
            "did_it_reveal_new_considerations": review.get("did_it_reveal_new_considerations"),
            "did_it_resolve_existing_tensions": review.get("did_it_resolve_existing_tensions"),
            "suggested_refinements": review.get("suggested_refinements", ""),
        })
        extra["assessment_rounds"] = rounds
        extra["score"] = review.get("score")
        extra["screening_passed"] = review.get("screening_passed")
        extra["stage"] = "validated" if self.phase == VALIDATION_PHASE else "screened"
        await self.db.update_page_extra(self.question_id, extra)
