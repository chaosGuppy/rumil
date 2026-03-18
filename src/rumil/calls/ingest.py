"""Ingest call: extract considerations from a source document."""

import logging

from rumil.calls.base import SimpleCall
from rumil.context import build_call_context, build_embedding_based_context
from rumil.database import DB
from rumil.models import Call, CallStage, CallType, Page

log = logging.getLogger(__name__)


class IngestCall(SimpleCall):
    """Ingest a source document: extract considerations for a question."""

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
        super().__init__(question_id, call, db, broadcaster=broadcaster, up_to_stage=up_to_stage)
        self.source_page = source_page
        extra = source_page.extra or {}
        self.filename = extra.get("filename", source_page.id[:8])

    def call_type(self) -> CallType:
        return CallType.INGEST

    def task_description(self) -> str:
        return (
            "Extract considerations from the source document above for this question.\n\n"
            f"Question ID: `{self.question_id}`\n"
            f"Source page ID: `{self.source_page.id}`"
        )

    def result_summary(self) -> str:
        return (
            f"Ingest complete. Created {len(self.result.created_page_ids)} "
            f"pages from '{self.filename}'."
        )

    async def build_context(self) -> None:
        question_context, _, self.working_page_ids = await build_call_context(
            self.question_id, self.db, extra_page_ids=self.preloaded_ids,
        )
        await self._record_context_built(source_page_id=self.source_page.id)

        source_section = (
            "\n\n---\n\n## Source Document\n\n"
            f"**File:** {self.filename}  \n"
            f"**Source page ID:** `{self.source_page.id}`\n\n"
            f"{self.source_page.content}"
        )
        self.context_text = question_context + source_section
        await self._load_phase1_pages()

    def _log_review(self, review: dict) -> None:
        log.info(
            "Ingest review: confidence=%s, remaining_fruit=%s",
            review.get("confidence_in_output", "?"),
            review.get("remaining_fruit", "?"),
        )


class EmbeddingIngestCall(IngestCall):
    """Ingest call that builds context via embedding similarity search."""

    async def build_context(self) -> None:
        question = await self.db.get_page(self.question_id)
        query = question.summary if question else self.question_id
        result = await build_embedding_based_context(query, self.db)
        self.working_page_ids = result.page_ids
        await self._record_context_built(source_page_id=self.source_page.id)

        source_section = (
            '\n\n---\n\n## Source Document\n\n'
            f'**File:** {self.filename}  \n'
            f'**Source page ID:** `{self.source_page.id}`\n\n'
            f'{self.source_page.content}'
        )
        self.context_text = result.context_text + source_section


