"""Assess call: synthesise considerations and render a judgement."""

from rumil.calls.closing_reviewers import StandardClosingReview
from rumil.calls.context_builders import BigAssessContext, EmbeddingContext
from rumil.calls.page_creators import SimpleAgentLoop
from rumil.calls.stages import CallRunner, ClosingReviewer, ContextBuilder, PageCreator
from rumil.models import CallType


class AssessCall(CallRunner):
    """Assess a question: weigh considerations and produce a judgement."""

    context_builder_cls = EmbeddingContext
    page_creator_cls = SimpleAgentLoop
    closing_reviewer_cls = StandardClosingReview
    call_type = CallType.ASSESS

    def _make_context_builder(self) -> ContextBuilder:
        return EmbeddingContext(
            self.call_type,
            require_judgement_for_questions=True,
        )

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


class BigAssessCall(AssessCall):
    """Assess call that freshens connected pages before embedding-based assessment.

    Resolves superseded links, reassesses stale dependencies, and checks for
    higher-quality replacement pages via embedding search before building
    the final assessment context.
    """

    context_builder_cls = BigAssessContext

    def __init__(self, *args, guidance: str = "", **kwargs) -> None:
        self._guidance = guidance
        super().__init__(*args, **kwargs)

    def _make_context_builder(self) -> ContextBuilder:
        return BigAssessContext(self.call_type)

    def _make_page_creator(self) -> PageCreator:
        return SimpleAgentLoop(
            self.call_type,
            self.task_description(),
            available_moves=self._resolve_available_moves(),
            prompt_name="big_assess",
        )

    def task_description(self) -> str:
        base = (
            "Assess this question and render a judgement.\n\n"
            f"Question ID: `{self.infra.question_id}`\n\n"
            "Follow the instructions in the system prompt."
        )
        if self._guidance:
            return base + f"\n\n## Guidance\n\n{self._guidance}"
        return base
