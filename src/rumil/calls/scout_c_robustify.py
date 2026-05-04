"""Scout Robustify call: suggest more robust variations of a claim."""

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


class ScoutCRobustifyCall(CallRunner):
    """Suggest more robust variations of the scope claim."""

    context_builder_cls = EmbeddingContext
    workspace_updater_cls = MultiRoundLoop
    closing_reviewer_cls = StandardClosingReview
    call_type = CallType.SCOUT_C_ROBUSTIFY

    def _make_context_builder(self) -> ContextBuilder:
        # No view_for_scout: this scout's scope is a claim, not a question,
        # so View.render_for_scout would just round-trip to no result.
        return EmbeddingContext(self.call_type)

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
            "Suggest variations of the scope claim that are more robustly "
            "true — e.g. lower bounds instead of point estimates, conditional "
            "claims, narrower scope where evidence is strongest, or weaker "
            "quantifiers. Each variation should be linked back to the original "
            "claim and still be substantive enough to be useful. Focus on "
            "variations genuinely different from ones already identified.\n\n"
            f"Claim ID: `{self.infra.question_id}`"
        )
