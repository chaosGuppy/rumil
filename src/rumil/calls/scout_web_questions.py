"""Scout Web Questions call: surface concrete factual questions answerable via web research."""

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


class ScoutWebQuestionsCall(CallRunner):
    """Surface concrete factual questions that web research could answer."""

    context_builder_cls = EmbeddingContext
    workspace_updater_cls = MultiRoundLoop
    closing_reviewer_cls = StandardClosingReview
    call_type = CallType.SCOUT_WEB_QUESTIONS

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
            "Identify concrete factual questions whose answers would bear "
            "on the scope question and that can be answered by reading the "
            "web, without judgement or tricky reasoning. Focus on questions "
            "where you do not already confidently know the answer. For each, "
            "create a question using `create_question` (it is automatically "
            "linked as a child of the scope question). Also produce "
            "confident, non-obvious factual claims that bear on the scope "
            "question.\n\n"
            f"Question ID: `{self.infra.question_id}`"
        )
