"""Scout Estimates call: identify informative quantities and make initial guesses."""

from rumil.calls.closing_reviewers import StandardClosingReview
from rumil.calls.context_builders import EmbeddingContext
from rumil.calls.page_creators import SimpleAgentLoop
from rumil.calls.stages import CallRunner, ClosingReviewer, ContextBuilder, PageCreator
from rumil.models import CallType, MoveType


class ScoutEstimatesCall(CallRunner):
    """Identify quantities whose estimates would be informative about the parent question."""

    context_builder_cls = EmbeddingContext
    page_creator_cls = SimpleAgentLoop
    closing_reviewer_cls = StandardClosingReview
    call_type = CallType.SCOUT_ESTIMATES
    available_moves = [
        MoveType.CREATE_CLAIM,
        MoveType.CREATE_SCOUT_QUESTION,
        MoveType.LINK_CONSIDERATION,
        MoveType.LINK_CHILD_QUESTION,
        MoveType.LOAD_PAGE,
    ]

    def _make_context_builder(self) -> ContextBuilder:
        return EmbeddingContext(self.call_type)

    def _make_page_creator(self) -> PageCreator:
        return SimpleAgentLoop(
            self.call_type,
            self.task_description(),
            available_moves=self._resolve_available_moves(),
        )

    def _make_closing_reviewer(self) -> ClosingReviewer:
        return StandardClosingReview(self.call_type)

    def task_description(self) -> str:
        return (
            "Identify quantities whose estimates would be highly informative "
            "about the parent question. Make initial guesses about their "
            "values as claims, and generate subquestions asking about those "
            "values so estimates can be refined.\n\n"
            f"Question ID: `{self.infra.question_id}`"
        )
