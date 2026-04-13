"""
WorldviewOrchestrator: explore/evaluate cycling driven by view health.
"""

import logging

from rumil.database import DB
from rumil.models import CallType, SuggestionType
from rumil.orchestrators.base import BaseOrchestrator
from rumil.orchestrators.common import (
    assess_question,
    find_considerations_until_done,
)
from rumil.tracing.broadcast import Broadcaster
from rumil.views import build_view

log = logging.getLogger(__name__)

EXPLORE_CALL_TYPES = {
    CallType.FIND_CONSIDERATIONS,
    CallType.WEB_RESEARCH,
    CallType.SCOUT_SUBQUESTIONS,
    CallType.SCOUT_ESTIMATES,
    CallType.SCOUT_HYPOTHESES,
    CallType.SCOUT_ANALOGIES,
    CallType.SCOUT_FACTCHECKS,
    CallType.SCOUT_WEB_QUESTIONS,
    CallType.SCOUT_DEEP_QUESTIONS,
}


class WorldviewOrchestrator(BaseOrchestrator):
    """Cycles between explore and evaluate modes based on view health.

    Explore mode dispatches find_considerations to expand research.
    Evaluate mode dispatches assess to recalibrate scores.
    Mode selection uses view health, pending suggestions, and recent
    call history to decide what the question needs next.
    """

    def __init__(
        self, db: DB,
        broadcaster: Broadcaster | None = None,
    ):
        super().__init__(db, broadcaster)

    async def _decide_mode(self, question_id: str) -> str:
        """Pick explore or evaluate based on the question's current state."""
        view = await build_view(self.db, question_id)

        if view.health.total_pages < 3:
            log.info("Mode decision: explore (too few pages: %d)", view.health.total_pages)
            return "explore"

        pending = await self.db.get_pending_suggestions()
        research_page_ids = {question_id}
        for section in view.sections:
            for item in section.items:
                research_page_ids.add(item.page.id)
        has_cascade = any(
            s.suggestion_type == SuggestionType.CASCADE_REVIEW
            and s.target_page_id in research_page_ids
            for s in pending
        )
        if has_cascade:
            log.info("Mode decision: evaluate (pending cascade reviews)")
            return "evaluate"

        judgements = await self.db.get_judgements_for_question(question_id)
        active_judgements = [j for j in judgements if j.is_active()]
        considerations = await self.db.get_considerations_for_question(question_id)
        active_considerations = [(p, l) for p, l in considerations if p.is_active()]

        if not active_judgements and len(active_considerations) >= 5:
            log.info(
                "Mode decision: evaluate (no judgement, %d considerations)",
                len(active_considerations),
            )
            return "evaluate"

        call_counts = await self.db.get_call_counts_by_type(question_id)
        explore_count = sum(
            call_counts.get(ct.value, 0) for ct in EXPLORE_CALL_TYPES
        )
        assess_count = call_counts.get(CallType.ASSESS.value, 0)
        if explore_count > assess_count and len(active_considerations) >= 3:
            log.info(
                "Mode decision: evaluate (last explored, %d considerations, "
                "explore=%d > assess=%d)",
                len(active_considerations), explore_count, assess_count,
            )
            return "evaluate"

        log.info("Mode decision: explore (default)")
        return "explore"

    async def run(self, root_question_id: str) -> None:
        await self._setup()
        try:
            while await self.db.budget_remaining() > 0:
                mode = await self._decide_mode(root_question_id)
                if mode == "evaluate":
                    log.info("Worldview: running assess on %s", root_question_id[:8])
                    await assess_question(
                        root_question_id,
                        self.db,
                        broadcaster=self.broadcaster,
                    )
                else:
                    log.info("Worldview: running find_considerations on %s", root_question_id[:8])
                    await find_considerations_until_done(
                        root_question_id,
                        self.db,
                        broadcaster=self.broadcaster,
                    )
        finally:
            await self._teardown()
