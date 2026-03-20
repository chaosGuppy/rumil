"""Scout Subquestions call: identify informative subquestions and initial considerations."""

import logging

from rumil.calls.base import SimpleCall
from rumil.context import build_embedding_based_context
from rumil.models import CallType, MoveType

log = logging.getLogger(__name__)


class ScoutSubquestionsCall(SimpleCall):
    """Identify subquestions whose answers would be highly informative about the parent."""

    def call_type(self) -> CallType:
        return CallType.SCOUT_SUBQUESTIONS

    def task_description(self) -> str:
        return (
            'Identify subquestions whose answers would be highly informative '
            'about the parent question, and generate initial considerations '
            'that bear on the question.\n\n'
            f'Question ID: `{self.question_id}`'
        )

    def result_summary(self) -> str:
        return (
            f'Scout subquestions complete. '
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
            MoveType.LOAD_PAGE,
        ]

    async def create_pages(self) -> None:
        self.available_moves = self._get_available_moves()
        await super().create_pages()
