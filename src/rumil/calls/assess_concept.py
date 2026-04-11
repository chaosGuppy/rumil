"""Assess Concept call: evaluate a staged concept proposal."""

from rumil.calls.assess_concept_types import (
    SCREENING_PHASE,
    SCREENING_MAX_ROUNDS,
    VALIDATION_PHASE,
    VALIDATION_MAX_ROUNDS,
    SCREENING_FRUIT_THRESHOLD,
    VALIDATION_FRUIT_THRESHOLD,
)
from rumil.calls.closing_reviewers import ConceptAssessReview
from rumil.calls.context_builders import ConceptAssessContext
from rumil.calls.page_creators import SimpleAgentLoop
from rumil.calls.stages import CallRunner, ClosingReviewer, ContextBuilder, WorkspaceUpdater
from rumil.database import DB
from rumil.models import Call, CallStage, CallType, MoveType


class AssessConceptCall(CallRunner):
    """Assess a staged concept proposal: one round of testing."""

    context_builder_cls = ConceptAssessContext
    workspace_updater_cls = SimpleAgentLoop
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

    def _make_workspace_updater(self) -> WorkspaceUpdater:
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
