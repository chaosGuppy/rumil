"""Scout Cruxes call: identify divergence points between how-true and how-false stories."""

from rumil.calls.closing_reviewers import StandardClosingReview
from rumil.calls.context_builders import EmbeddingContext
from rumil.calls.page_creators import MultiRoundLoop
from rumil.calls.stages import CallRunner, ClosingReviewer, ContextBuilder, PageCreator
from rumil.models import CallType


class ScoutCCruxesCall(CallRunner):
    """Identify cruxes where how-true and how-false stories diverge."""

    context_builder_cls = EmbeddingContext
    page_creator_cls = MultiRoundLoop
    closing_reviewer_cls = StandardClosingReview
    call_type = CallType.SCOUT_C_CRUXES

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
            "Identify cruxes — specific points where the how-true and "
            "how-false stories diverge, such that resolving them would "
            "tell you which story is closer to the truth. A crux may be "
            "a claim (something whose truth is load-bearing) or a question "
            "(something whose answer would discriminate between stories). "
            "Rank by importance and tractability.\n\n"
            f"Claim ID: `{self.infra.question_id}`"
        )
