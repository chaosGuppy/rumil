"""Assess Concept call: evaluate a staged concept proposal."""

from pydantic import BaseModel, Field

from rumil.calls.closing_reviewers import ConceptAssessReview
from rumil.calls.context_builders import ConceptAssessContext
from rumil.calls.page_creators import SimpleAgentLoop
from rumil.calls.stages import CallRunner, ClosingReviewer, ContextBuilder, PageCreator
from rumil.database import DB
from rumil.models import Call, CallStage, CallType, MoveType

SCREENING_PHASE = 'screening'
VALIDATION_PHASE = 'validation'

SCREENING_MAX_ROUNDS = 2
VALIDATION_MAX_ROUNDS = 8
SCREENING_FRUIT_THRESHOLD = 5
VALIDATION_FRUIT_THRESHOLD = 3


class ConceptAssessmentReview(BaseModel):
    remaining_fruit: int = Field(
        description=(
            '0-10: how much more testing would meaningfully change your assessment. '
            '0 = verdict is settled; 10 = barely started.'
        )
    )
    confidence_in_output: float = Field(description='0-5 confidence in this assessment')
    score: int = Field(
        description=(
            '1-10: overall usefulness of this concept for the research. '
            '1-4 = not useful enough to warrant promotion; '
            '5-7 = moderately useful, borderline; '
            '8-10 = clearly useful, strong candidate for promotion.'
        )
    )
    what_worked: str = Field(description='Where the concept added clarity or revealed something')
    what_didnt: str = Field(description='Where the concept failed or added noise')
    could_existing_claims_be_restated: bool = Field(
        description='Whether existing claims could be stated more usefully with this concept'
    )
    did_it_reveal_new_considerations: bool = Field(
        description='Whether applying the concept surfaced considerations not already in the workspace'
    )
    did_it_resolve_existing_tensions: bool = Field(
        description='Whether the concept dissolved any apparent contradictions or tensions'
    )
    suggested_refinements: str = Field(
        '', description='How the concept could be sharpened or narrowed to be more useful'
    )
    screening_passed: bool = Field(
        description=(
            'Whether this concept warrants deeper validation. '
            'True if there is genuine promise, even if uncertain. '
            'False if the concept clearly does not add value.'
        )
    )


REVIEW_SYSTEM_PROMPT = (
    'You are a research assistant completing a closing review of a concept assessment '
    'you just performed. Be honest and specific. Most concept proposals should not be '
    "promoted \u2014 a clear 'no' is more useful than an uncertain 'maybe'."
)


class AssessConceptCall(CallRunner):
    """Assess a staged concept proposal: one round of testing."""

    context_builder_cls = ConceptAssessContext
    page_creator_cls = SimpleAgentLoop
    closing_reviewer_cls = ConceptAssessReview
    call_type = CallType.ASSESS_CONCEPT

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
        self._phase = phase
        super().__init__(
            concept_page_id, call, db,
            broadcaster=broadcaster, up_to_stage=up_to_stage,
        )

    @property
    def concept_assessment(self) -> dict:
        reviewer = self.closing_reviewer
        if isinstance(reviewer, ConceptAssessReview):
            return reviewer.concept_assessment
        return {}

    def _make_context_builder(self) -> ContextBuilder:
        return ConceptAssessContext(self._phase)

    def _make_page_creator(self) -> PageCreator:
        if self._phase == VALIDATION_PHASE:
            moves = [MoveType.PROMOTE_CONCEPT, MoveType.LOAD_PAGE]
        else:
            moves = [MoveType.LOAD_PAGE]
        return SimpleAgentLoop(
            self.call_type, self.task_description(),
            available_moves=moves,
        )

    def _make_closing_reviewer(self) -> ClosingReviewer:
        return ConceptAssessReview(self._phase)

    def task_description(self) -> str:
        phase_note = (
            'This is the **screening phase** \u2014 form a quick initial verdict. '
            'Load a few representative pages and test the concept against them. '
            'Set `screening_passed` based on whether you see genuine potential.'
            if self._phase == SCREENING_PHASE
            else
            'This is the **validation phase** \u2014 do thorough testing. '
            'Load more pages, try to restate claims through the concept lens, '
            'look for edge cases. '
            'Call `promote_concept` if and only if you are confident it earns its place.'
        )
        return (
            f'Assess the concept proposal above.\n\n{phase_note}\n\n'
            f'Concept page ID: `{self.infra.question_id}`'
        )
