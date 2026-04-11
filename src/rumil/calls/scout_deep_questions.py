"""Scout Deep Questions call: surface important questions requiring judgement or involved reasoning."""

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


class ScoutDeepQuestionsCall(CallRunner):
    """Surface important questions that require judgement or involved reasoning."""

    context_builder_cls = EmbeddingContext
    workspace_updater_cls = MultiRoundLoop
    closing_reviewer_cls = StandardClosingReview
    call_type = CallType.SCOUT_DEEP_QUESTIONS

    def _make_context_builder(self) -> ContextBuilder:
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
            "Identify important questions bearing on the scope question that "
            "require judgement, interpretation, or involved reasoning to answer "
            "— questions that cannot be resolved by simply looking something up. "
            "For each, create a question and link it as a child of the scope "
            "question. Also produce confident, non-obvious high-level claims "
            "that bear on the scope question.\n\n"
            f"Question ID: `{self.infra.question_id}`"
        )
