"""Scout Estimates call: identify informative quantities and make initial guesses."""

import logging

from rumil.calls.base import SimpleCall
from rumil.context import build_call_context
from rumil.models import CallType, MoveType
from rumil.page_graph import PageGraph

log = logging.getLogger(__name__)


class ScoutEstimatesCall(SimpleCall):
    """Identify quantities whose estimates would be informative about the parent question."""

    def call_type(self) -> CallType:
        return CallType.SCOUT_ESTIMATES

    def task_description(self) -> str:
        return (
            'Identify quantities whose estimates would be highly informative '
            'about the parent question. Make initial guesses about their '
            'values as claims, and generate subquestions asking about those '
            'values so estimates can be refined.\n\n'
            f'Question ID: `{self.question_id}`'
        )

    def result_summary(self) -> str:
        return (
            f'Scout estimates complete. '
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
