"""Scout Facts-to-Check call: surface uncertain facts whose truth value bears on the question."""

from rumil.calls.closing_reviewers import StandardClosingReview
from rumil.calls.context_builders import EmbeddingContext
from rumil.calls.page_creators import SimpleAgentLoop
from rumil.calls.stages import CallRunner, ClosingReviewer, ContextBuilder, PageCreator
from rumil.models import CallType, MoveType


class ScoutFactsToCheckCall(CallRunner):
    """Surface checkable facts the model is uncertain about that bear on the question."""

    context_builder_cls = EmbeddingContext
    page_creator_cls = SimpleAgentLoop
    closing_reviewer_cls = StandardClosingReview
    call_type = CallType.SCOUT_FACTS_TO_CHECK
    available_moves = [
        MoveType.CREATE_QUESTION,
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
            "Identify factual claims, figures, or examples in the workspace "
            "that would benefit from web-based verification. For each, create "
            "a question that a web researcher could answer — either verifying "
            "a specific assertion, finding the actual value of a quantity, or "
            "searching for known examples of a type.\n\n"
            f"Question ID: `{self.infra.question_id}`"
        )
