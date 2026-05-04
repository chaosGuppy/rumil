"""Scout Relevant Evidence call: identify evidence questions bearing on cruxes."""

from rumil.calls.closing_reviewers import StandardClosingReview
from rumil.calls.context_builders import EmbeddingContext
from rumil.calls.page_creators import MultiRoundLoop
from rumil.calls.stages import (
    CallRunner,
    ClosingReviewer,
    ContextBuilder,
    WorkspaceUpdater,
)
from rumil.models import CallType


class ScoutCRelevantEvidenceCall(CallRunner):
    """Identify evidence worth gathering that bears on important cruxes."""

    context_builder_cls = EmbeddingContext
    workspace_updater_cls = MultiRoundLoop
    closing_reviewer_cls = StandardClosingReview
    call_type = CallType.SCOUT_C_RELEVANT_EVIDENCE

    def _make_context_builder(self) -> ContextBuilder:
        return EmbeddingContext(self.call_type, view_for_scout=True)

    def _make_workspace_updater(self) -> WorkspaceUpdater:
        return MultiRoundLoop(
            self._max_rounds,
            self._fruit_threshold,
            available_moves=self._resolve_available_moves(),
            call_type=self.call_type,
            task_description=self.task_description(),
        )

    def _make_closing_reviewer(self) -> ClosingReviewer:
        return StandardClosingReview(self.call_type)

    def task_description(self) -> str:
        return (
            "Identify evidence worth gathering that bears on the most "
            "important cruxes of the scope claim. Frame each as a "
            'question: "What does the literature say about X?", "What is '
            'the actual rate of Y?", "Are there documented cases of Z?" '
            "Prioritize questions that would discriminate between stories "
            "over questions whose answers would merely be consistent with "
            "one.\n\n"
            f"Claim ID: `{self.infra.question_id}`"
        )
