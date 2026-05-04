"""Scout Strengthen call: make a high-credence claim more precise or specific."""

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


class ScoutCStrengthenCall(CallRunner):
    """Make a high-credence claim more precise or specific."""

    context_builder_cls = EmbeddingContext
    workspace_updater_cls = MultiRoundLoop
    closing_reviewer_cls = StandardClosingReview
    call_type = CallType.SCOUT_C_STRENGTHEN

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
            "The scope claim already has high credence. Try to make it more "
            "precise, specific, or stronger while maintaining that credence. "
            "Add quantitative bounds, narrow error bars, strengthen quantifiers "
            "where evidence supports it, or add specificity. Each variation "
            "should be linked back to the original claim.\n\n"
            f"Claim ID: `{self.infra.question_id}`"
        )
