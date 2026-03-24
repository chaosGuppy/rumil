"""Scout Analogies call: identify informative analogies for the question."""

from rumil.calls.closing_reviewers import StandardClosingReview
from rumil.calls.context_builders import EmbeddingContext
from rumil.calls.page_creators import MultiRoundLoop
from rumil.calls.stages import CallRunner, ClosingReviewer, ContextBuilder, PageCreator
from rumil.models import CallType, MoveType


class ScoutAnalogiesCall(CallRunner):
    """Identify analogies that may be informative about the question."""

    context_builder_cls = EmbeddingContext
    page_creator_cls = MultiRoundLoop
    closing_reviewer_cls = StandardClosingReview
    call_type = CallType.SCOUT_ANALOGIES
    available_moves = [
        MoveType.CREATE_CLAIM,
        MoveType.CREATE_SCOUT_QUESTION,
        MoveType.LINK_CONSIDERATION,
        MoveType.LINK_CHILD_QUESTION,
        MoveType.LINK_RELATED,
        MoveType.LOAD_PAGE,
    ]

    def _make_context_builder(self) -> ContextBuilder:
        return EmbeddingContext(self.call_type)

    def _make_page_creator(self) -> PageCreator:
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
            "Identify analogies that may be informative about the parent "
            "question. For each analogy, create claims describing it and its "
            "relevance, and generate subquestions asking about the details "
            "and limits of the analogy.\n\n"
            f"Question ID: `{self.infra.question_id}`"
        )
