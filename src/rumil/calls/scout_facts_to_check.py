"""Scout Facts-to-Check call: surface uncertain facts whose truth value bears on the question."""

from rumil.calls.closing_reviewers import StandardClosingReview
from rumil.calls.context_builders import EmbeddingContext
from rumil.calls.page_creators import MultiRoundLoop
from rumil.calls.stages import CallRunner, ClosingReviewer, ContextBuilder, PageCreator
from rumil.models import CallType, MoveType


class ScoutFactsToCheckCall(CallRunner):
    """Surface checkable facts the model is uncertain about that bear on the question."""

    context_builder_cls = EmbeddingContext
    page_creator_cls = MultiRoundLoop
    closing_reviewer_cls = StandardClosingReview
    call_type = CallType.SCOUT_FACTS_TO_CHECK
    available_moves = [
        MoveType.CREATE_CLAIM,
        MoveType.CREATE_QUESTION,
        MoveType.LINK_CONSIDERATION,
        MoveType.LINK_CHILD_QUESTION,
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
            "Identify facts you are uncertain about whose truth value "
            "could materially affect the answer to the question, and "
            "create subquestions so they can be verified.\n\n"
            f"Question ID: `{self.infra.question_id}`"
        )
