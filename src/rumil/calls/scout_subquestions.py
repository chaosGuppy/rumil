"""Scout Subquestions call: identify informative subquestions and initial considerations."""

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


class ScoutSubquestionsCall(CallRunner):
    """Identify subquestions whose answers would be highly informative about the parent."""

    context_builder_cls = EmbeddingContext
    workspace_updater_cls = MultiRoundLoop
    closing_reviewer_cls = StandardClosingReview
    call_type = CallType.SCOUT_SUBQUESTIONS

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
            "Identify subquestions whose answers would be highly informative "
            "about the parent question, and generate initial considerations "
            "that bear on the question.\n\n"
            f"Question ID: `{self.infra.question_id}`"
        )
