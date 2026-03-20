"""Scout Paradigm Cases call: identify concrete cases that illuminate the question."""

import logging

from rumil.calls.base import SimpleCall
from rumil.context import build_embedding_based_context
from rumil.models import CallType, MoveType

log = logging.getLogger(__name__)


class ScoutParadigmCasesCall(SimpleCall):
    """Identify concrete cases or examples that illuminate the question."""

    def call_type(self) -> CallType:
        return CallType.SCOUT_PARADIGM_CASES

    def task_description(self) -> str:
        return (
            'Identify paradigm cases — concrete, real-world examples that '
            'illuminate the parent question. For each case, create claims '
            'describing it and its relevance, and generate subquestions '
            'asking about its details and implications.\n\n'
            f'Question ID: `{self.question_id}`'
        )

    def result_summary(self) -> str:
        return (
            f'Scout paradigm cases complete. '
            f'Created {len(self.result.created_page_ids)} pages.'
        )

    async def build_context(self) -> None:
        question = await self.db.get_page(self.question_id)
        query = question.headline if question else self.question_id
        result = await build_embedding_based_context(
            query, self.db, scope_question_id=self.question_id,
        )
        self.context_text = result.context_text
        self.working_page_ids = result.page_ids
        await self._record_context_built()

    def _get_available_moves(self) -> list[MoveType]:
        return [
            MoveType.CREATE_CLAIM,
            MoveType.CREATE_QUESTION,
            MoveType.LINK_CONSIDERATION,
            MoveType.LINK_CHILD_QUESTION,
            MoveType.LINK_RELATED,
            MoveType.LOAD_PAGE,
        ]

    async def create_pages(self) -> None:
        self.available_moves = self._get_available_moves()
        await super().create_pages()
