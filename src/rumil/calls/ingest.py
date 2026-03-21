"""Ingest call: extract considerations from a source document."""

from rumil.calls.closing_reviewers import IngestClosingReview
from rumil.calls.context_builders import IngestEmbeddingContext, IngestGraphContext
from rumil.calls.page_creators import SimpleAgentLoop
from rumil.calls.stages import CallRunner, ClosingReviewer, ContextBuilder, PageCreator
from rumil.database import DB
from rumil.models import Call, CallStage, CallType, Page


class IngestCall(CallRunner):
    """Ingest a source document: extract considerations for a question."""

    context_builder_cls = IngestGraphContext
    page_creator_cls = SimpleAgentLoop
    closing_reviewer_cls = IngestClosingReview
    call_type = CallType.INGEST

    def __init__(
        self,
        source_page: Page,
        question_id: str,
        call: Call,
        db: DB,
        *,
        broadcaster=None,
        up_to_stage: CallStage | None = None,
    ):
        self._source_page = source_page
        extra = source_page.extra or {}
        self._filename = extra.get('filename', source_page.id[:8])
        super().__init__(question_id, call, db, broadcaster=broadcaster, up_to_stage=up_to_stage)

    def _make_context_builder(self) -> ContextBuilder:
        return IngestGraphContext(self._source_page)

    def _make_page_creator(self) -> PageCreator:
        return SimpleAgentLoop(
            self.call_type, self.task_description(),
        )

    def _make_closing_reviewer(self) -> ClosingReviewer:
        return IngestClosingReview(self.call_type, self._filename)

    def task_description(self) -> str:
        return (
            'Extract considerations from the source document above for this question.\n\n'
            f'Question ID: `{self.infra.question_id}`\n'
            f'Source page ID: `{self._source_page.id}`'
        )


class EmbeddingIngestCall(IngestCall):
    """Ingest call that builds context via embedding similarity search."""

    context_builder_cls = IngestEmbeddingContext

    def _make_context_builder(self) -> ContextBuilder:
        return IngestEmbeddingContext(self._source_page)
