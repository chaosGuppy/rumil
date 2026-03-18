"""Scout Concepts call: identify concept proposals from the research workspace."""

import logging

from rumil.calls.base import SimpleCall
from rumil.context import build_call_context
from rumil.models import CallType, MoveType
from rumil.page_graph import PageGraph

log = logging.getLogger(__name__)


class ScoutConceptsCall(SimpleCall):
    """Survey the research workspace and propose concepts for assessment."""

    def call_type(self) -> CallType:
        return CallType.SCOUT_CONCEPTS

    def task_description(self) -> str:
        return (
            "Survey the research workspace and the concept registry above. "
            "Identify 1-3 concepts or distinctions that would meaningfully "
            "clarify the investigation. Use `propose_concept` to record each proposal."
        )

    def result_summary(self) -> str:
        return (
            f"Scout concepts complete. "
            f"Proposed {len(self.result.created_page_ids)} concept(s)."
        )

    async def build_context(self) -> None:
        graph = await PageGraph.load(self.db)
        self.context_text, _, self.working_page_ids = await build_call_context(
            self.question_id, self.db, extra_page_ids=self.preloaded_ids,
            graph=graph,
        )

        registry = await self.db.get_concept_registry()
        if registry:
            lines = ["## Concept Registry", ""]
            lines.append(
                "The following concepts have already been proposed (do not re-propose them):"
            )
            lines.append("")
            for concept in registry:
                extra = concept.extra or {}
                stage = extra.get("stage", "proposed")
                score = extra.get("score")
                promoted = extra.get("promoted", False)
                status = "promoted" if promoted else (
                    f"score={score}" if score is not None else stage
                )
                lines.append(
                    f"- [{status}] `{concept.id[:8]}` — {concept.headline}"
                )
            self.context_text += "\n\n" + "\n".join(lines)

        await self._record_context_built()
        await self._load_phase1_pages()

    def _get_available_moves(self) -> list[MoveType]:
        return [MoveType.PROPOSE_CONCEPT, MoveType.LOAD_PAGE]

    async def create_pages(self) -> None:
        self.available_moves = self._get_available_moves()
        await super().create_pages()
