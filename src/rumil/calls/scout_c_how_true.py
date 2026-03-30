"""Scout How-True call: identify causal mechanisms that would make a claim true."""

from rumil.calls.closing_reviewers import StandardClosingReview
from rumil.calls.context_builders import EmbeddingContext
from rumil.calls.page_creators import MultiRoundLoop
from rumil.calls.stages import CallRunner, ClosingReviewer, ContextBuilder, PageCreator
from rumil.models import CallType


class ScoutCHowTrueCall(CallRunner):
    """Identify plausible causal mechanisms that would make the claim true."""

    context_builder_cls = EmbeddingContext
    page_creator_cls = MultiRoundLoop
    closing_reviewer_cls = StandardClosingReview
    call_type = CallType.SCOUT_C_HOW_TRUE

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
            "Identify plausible causal mechanisms that would make the "
            "scope claim true. For each story: what is the mechanism? "
            "What is actually going on in the world that makes the claim "
            "true? What observable consequences would we expect if this "
            "mechanism is operating? Be specific and concrete about the "
            "causal chain. Focus on stories genuinely different from ones "
            "already identified.\n\n"
            f"Claim ID: `{self.infra.question_id}`"
        )
