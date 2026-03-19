"""Scout Subquestions call: identify informative subquestions and initial considerations."""

import logging

from rumil.calls.base import SimpleCall
from rumil.context import build_call_context
from rumil.models import CallType, MoveType
from rumil.page_graph import PageGraph

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
        graph = await PageGraph.load(self.db)
        self.context_text, _, self.working_page_ids = await build_call_context(
            self.question_id, self.db, extra_page_ids=self.preloaded_ids,
            graph=graph,
        )
        await self._record_context_built()
        await self._load_phase1_pages()

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
