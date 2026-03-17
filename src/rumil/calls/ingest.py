"""Ingest call: extract considerations from a source document."""

import logging

from rumil.calls.base import SimpleCall
from rumil.calls.common import RunCallResult
from rumil.context import build_call_context
from rumil.database import DB
from rumil.models import Call, CallType, Page

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
    ):
        super().__init__(question_id, call, db, broadcaster=broadcaster)
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


async def run_ingest(
    source_page: Page,
    question_id: str,
    call: Call,
    db: DB,
    broadcaster=None,
) -> tuple[RunCallResult, dict]:
    """Run an Ingest call: extract considerations from a source document.

    Returns (run_call_result, review_dict).
    """
    extra = source_page.extra or {}
    filename = extra.get("filename", source_page.id[:8])
    log.info(
        "Ingest starting: call=%s, source=%s (%s), question=%s",
        call.id[:8], source_page.id[:8], filename, question_id[:8],
    )
    ingest = IngestCall(source_page, question_id, call, db, broadcaster=broadcaster)
    await ingest.run()
    log.info(
        "Ingest complete: call=%s, pages_created=%d, source=%s",
        call.id[:8], len(ingest.result.created_page_ids), filename,
    )
    return ingest.result, ingest.review
