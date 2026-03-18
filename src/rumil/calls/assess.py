"""Assess call: synthesise considerations and render a judgement."""

import logging

from rumil.calls.base import SimpleCall
from rumil.context import build_call_context, build_embedding_based_context
from rumil.models import CallType
from rumil.page_graph import PageGraph

log = logging.getLogger(__name__)


class AssessCall(SimpleCall):
    """Assess a question: weigh considerations and produce a judgement."""

    def call_type(self) -> CallType:
        return CallType.ASSESS

    def task_description(self) -> str:
        return (
            "Assess this question and render a judgement.\n\n"
            f"Question ID: `{self.question_id}`\n\n"
            "Synthesise the considerations, weigh evidence on multiple sides, "
            "and produce a judgement with structured confidence. "
            "Even if uncertain, commit to a position."
        )

    def result_summary(self) -> str:
        return f"Assess complete. Created {len(self.result.created_page_ids)} pages."

    async def build_context(self) -> None:
        graph = await PageGraph.load(self.db)
        self.context_text, _, self.working_page_ids = await build_call_context(
            self.question_id, self.db, extra_page_ids=self.preloaded_ids,
            graph=graph,
        )
        await self._record_context_built()
        await self._load_phase1_pages()

    def _log_review(self, review: dict) -> None:
        log.info(
            "Assess review: confidence=%s, self_assessment=%s",
            review.get("confidence_in_output", "?"),
            review.get("self_assessment", "")[:80],
        )


class EmbeddingAssessCall(AssessCall):
    """Assess call that builds context via embedding similarity search."""

    async def build_context(self) -> None:
        question = await self.db.get_page(self.question_id)
        query = question.headline if question else self.question_id
        result = await build_embedding_based_context(
            query, self.db, scope_question_id=self.question_id,
        )
        self.context_text = result.context_text
        self.working_page_ids = result.page_ids
        await self._record_context_built()


