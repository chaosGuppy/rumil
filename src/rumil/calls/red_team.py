"""Red team call: structural challenge of the overall research picture."""

from rumil.calls.closing_reviewers import StandardClosingReview
from rumil.calls.context_builders import RedTeamContext
from rumil.calls.page_creators import SimpleAgentLoop
from rumil.calls.stages import (
    CallRunner,
    ClosingReviewer,
    ContextBuilder,
    WorkspaceUpdater,
)
from rumil.models import CallType


class RedTeamCall(CallRunner):
    """Challenge the overall framing and structure of the investigation."""

    context_builder_cls = RedTeamContext
    workspace_updater_cls = SimpleAgentLoop
    closing_reviewer_cls = StandardClosingReview
    call_type = CallType.RED_TEAM

    def _make_context_builder(self) -> ContextBuilder:
        return RedTeamContext()

    def _make_workspace_updater(self) -> WorkspaceUpdater:
        return SimpleAgentLoop(
            self.call_type,
            self.task_description(),
            available_moves=self._resolve_available_moves(),
        )

    def _make_closing_reviewer(self) -> ClosingReviewer:
        return StandardClosingReview(self.call_type)

    def task_description(self) -> str:
        return (
            "Red-team the overall research picture on this question. "
            "Challenge the framing, identify systematic blind spots, "
            "and surface structural weaknesses in the investigation — "
            "not individual claim details, but how the picture as a whole "
            "could be misleading.\n\n"
            f"Question ID: `{self.infra.question_id}`"
        )
