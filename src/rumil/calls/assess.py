"""Assess call: synthesise considerations and render a judgement."""

from rumil.calls.closing_reviewers import StandardClosingReview
from rumil.calls.context_builders import EmbeddingContext, GraphContextWithPhase1
from rumil.calls.page_creators import SimpleAgentLoop
from rumil.calls.stages import CallRunner, ClosingReviewer, ContextBuilder, PageCreator
from rumil.models import CallType


class AssessCall(CallRunner):
    """Assess a question: weigh considerations and produce a judgement."""

    context_builder_cls = GraphContextWithPhase1
    page_creator_cls = SimpleAgentLoop
    closing_reviewer_cls = StandardClosingReview
    call_type = CallType.ASSESS

    def _make_context_builder(self) -> ContextBuilder:
        return GraphContextWithPhase1(self.call_type)

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
            "Assess this question and render a judgement.\n\n"
            f"Question ID: `{self.infra.question_id}`\n\n"
            "Synthesise the considerations, weigh evidence on multiple sides, "
            "and produce a judgement with structured confidence. "
            "Even if uncertain, commit to a position."
        )


class EmbeddingAssessCall(AssessCall):
    """Assess call that builds context via embedding similarity search."""

    context_builder_cls = EmbeddingContext

    def _make_context_builder(self) -> ContextBuilder:
        return EmbeddingContext(
            self.call_type,
            require_judgement_for_questions=True,
        )
