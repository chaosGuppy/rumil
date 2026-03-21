"""Web research call: search the web and create source-grounded claims."""

from collections.abc import Sequence

from rumil.calls.closing_reviewers import WebResearchClosingReview
from rumil.calls.context_builders import WebResearchEmbeddingContext
from rumil.calls.page_creators import WebResearchLoop
from rumil.calls.stages import CallRunner, ClosingReviewer, ContextBuilder, PageCreator
from rumil.database import DB
from rumil.models import Call, CallStage, CallType


class WebResearchCall(CallRunner):
    """Web research call: search and fetch web sources, create grounded claims."""

    context_builder_cls = WebResearchEmbeddingContext
    page_creator_cls = WebResearchLoop
    closing_reviewer_cls = WebResearchClosingReview
    call_type = CallType.WEB_RESEARCH

    def __init__(
        self,
        question_id: str,
        call: Call,
        db: DB,
        *,
        allowed_domains: Sequence[str] | None = None,
        broadcaster=None,
        up_to_stage: CallStage | None = None,
    ):
        self._allowed_domains = allowed_domains
        super().__init__(
            question_id, call, db,
            broadcaster=broadcaster, up_to_stage=up_to_stage,
        )

    def _make_context_builder(self) -> ContextBuilder:
        return WebResearchEmbeddingContext()

    def _make_page_creator(self) -> PageCreator:
        return WebResearchLoop(allowed_domains=self._allowed_domains)

    def _make_closing_reviewer(self) -> ClosingReviewer:
        assert isinstance(self.page_creator, WebResearchLoop)
        return WebResearchClosingReview(self.call_type, self.page_creator)

    def task_description(self) -> str:
        return (
            'Search the web for evidence relevant to this question and create '
            'source-grounded claims.\n\n'
            'Question ID (use this when linking considerations): '
            f'`{self.infra.question_id}`'
        )
