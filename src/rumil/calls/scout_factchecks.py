"""Scout Factchecks call: identify factual claims that should be verified via web search."""

import logging

from rumil.calls.base import SimpleCall
from rumil.context import build_embedding_based_context
from rumil.models import CallType, MoveType

log = logging.getLogger(__name__)


class ScoutFactchecksCall(SimpleCall):
    """Identify factual claims in the workspace that should be verified via web research."""

    def call_type(self) -> CallType:
        return CallType.SCOUT_FACTCHECKS

    def task_description(self) -> str:
        return (
            'Identify factual claims, figures, or examples in the workspace '
            'that would benefit from web-based verification. For each, create '
            'a question that a web researcher could answer — either verifying '
            'a specific assertion, finding the actual value of a quantity, or '
            'searching for known examples of a type.\n\n'
            f'Question ID: `{self.question_id}`'
        )

    def result_summary(self) -> str:
        return (
            f'Scout factchecks complete. '
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
            MoveType.CREATE_QUESTION,
            MoveType.LINK_CHILD_QUESTION,
            MoveType.LOAD_PAGE,
        ]

    async def create_pages(self) -> None:
        self.available_moves = self._get_available_moves()
        await super().create_pages()
