"""CritiqueFirstOrchestrator: scout-gated find_considerations loop.

Design
------
The default loop is ``find_considerations -> assess -> distill``. The
WorldviewOrchestrator alternates explore/evaluate modes. This variant
inverts the usual order: before committing to a fresh round of
consideration-finding, run adversarial probes (``scout_c_how_true`` and
``scout_c_how_false``) whose output becomes framing context for the next
``find_considerations`` call.

Loop
----
On each cycle for the scope question ``Q``:

1. If ``Q`` has fewer than ``MIN_CONSIDERATIONS`` (default 3) considerations,
   run ``find_considerations`` to grow the base. This bootstraps the graph
   so the adversarial scouts have something to bite on.
2. Once the base is present, dispatch ``scout_c_how_true`` and
   ``scout_c_how_false`` (in parallel) against the strongest existing
   judgement if one exists (adversarial probes are cheaper and more
   focused when aimed at a specific claim), otherwise against the
   question itself.
3. Run ``find_considerations`` again with the scout output ids passed
   through as ``context_page_ids`` so the next round of
   consideration-finding is framed by the critique.
4. Run ``assess_question`` to let the critique feed back into credence/
   robustness.
5. Every ``VIEW_UPDATE_CADENCE`` cycles, refresh the question's view.
6. Terminate on budget exhaustion, when scouts produce no new pages two
   cycles in a row, or when the orchestrator exceeds ``MAX_CYCLES``.

"Strongest existing judgement" — picked as the active judgement with the
highest ``(credence or 0) * (robustness or 0)`` product, falling back to
the most recent if all scores are missing.
"""

import asyncio
import logging

from rumil.calls.scout_c_how_false import ScoutCHowFalseCall
from rumil.calls.scout_c_how_true import ScoutCHowTrueCall
from rumil.constants import MIN_TWOPHASE_BUDGET
from rumil.database import DB
from rumil.models import CallType, Page
from rumil.orchestrators.base import BaseOrchestrator
from rumil.orchestrators.common import (
    assess_question,
    check_triage_before_run,
    create_view_for_question,
    find_considerations_until_done,
    update_view_for_question,
)
from rumil.tracing.broadcast import Broadcaster

log = logging.getLogger(__name__)

MIN_CONSIDERATIONS = 3
VIEW_UPDATE_CADENCE = 3
MAX_CYCLES = 20
SCOUT_MAX_ROUNDS = 2
SCOUT_FRUIT_THRESHOLD = 4


class CritiqueFirstOrchestrator(BaseOrchestrator):
    """Scout-gated find_considerations loop.

    See module docstring for loop design.
    """

    def __init__(
        self,
        db: DB,
        broadcaster: Broadcaster | None = None,
    ):
        super().__init__(db, broadcaster)
        self._parent_call_id: str | None = None
        self._last_scout_page_ids: list[str] = []
        self._barren_scout_rounds: int = 0

    async def run(self, root_question_id: str) -> None:
        if not await check_triage_before_run(self.db, root_question_id):
            return
        own_db = await self.db.fork()
        self.db = own_db
        await self._setup()
        try:
            remaining = await self.db.budget_remaining()
            if remaining < MIN_TWOPHASE_BUDGET:
                raise ValueError(
                    "CritiqueFirstOrchestrator requires a budget of at least "
                    f"{MIN_TWOPHASE_BUDGET}, got {remaining}"
                )
            await self._loop(root_question_id)
        finally:
            await self._teardown()
            await own_db.close()

    async def _loop(self, question_id: str) -> None:
        cycle = 0
        while cycle < MAX_CYCLES:
            if await self.db.budget_remaining() <= 0:
                log.info("CritiqueFirstOrchestrator: budget exhausted, stopping")
                break
            cycle += 1
            log.info(
                "CritiqueFirstOrchestrator cycle %d: question=%s",
                cycle,
                question_id[:8],
            )

            considerations = await self.db.get_considerations_for_question(question_id)
            n_considerations = len(considerations)

            if n_considerations < MIN_CONSIDERATIONS:
                log.info(
                    "Only %d considerations (<%d), bootstrapping with find_considerations",
                    n_considerations,
                    MIN_CONSIDERATIONS,
                )
                await self._run_find_considerations(question_id, context_page_ids=None)
                if await self.db.budget_remaining() <= 0:
                    break
                await self._run_assess(question_id)
                await self._maybe_update_view(question_id, cycle)
                continue

            scout_target = await self._pick_scout_target(question_id)
            new_pages = await self._run_critique_scouts(scout_target)

            if not new_pages:
                self._barren_scout_rounds += 1
                if self._barren_scout_rounds >= 2:
                    log.info(
                        "CritiqueFirstOrchestrator: scouts produced no new pages "
                        "for %d rounds, stopping",
                        self._barren_scout_rounds,
                    )
                    break
            else:
                self._barren_scout_rounds = 0
                self._last_scout_page_ids = list(new_pages)

            if await self.db.budget_remaining() <= 0:
                break

            await self._run_find_considerations(
                question_id,
                context_page_ids=list(self._last_scout_page_ids) or None,
            )
            if await self.db.budget_remaining() <= 0:
                break
            await self._run_assess(question_id)
            await self._maybe_update_view(question_id, cycle)

    async def _pick_scout_target(self, question_id: str) -> str:
        """Prefer the strongest existing judgement; fall back to the question."""
        judgements = await self.db.get_judgements_for_question(question_id)
        if not judgements:
            return question_id
        active = [j for j in judgements if j.is_active()]
        if not active:
            return question_id

        def score(j: Page) -> tuple[int, float]:
            cr = (j.credence or 0) * (j.robustness or 0)
            return (cr, j.created_at.timestamp() if j.created_at else 0.0)

        strongest = max(active, key=score)
        log.info(
            "Scout target: judgement %s (credence=%s, robustness=%s)",
            strongest.id[:8],
            strongest.credence,
            strongest.robustness,
        )
        return strongest.id

    async def _run_critique_scouts(self, target_id: str) -> list[str]:
        """Run how-true and how-false scouts in parallel. Returns new page IDs."""
        remaining = await self.db.budget_remaining()
        if remaining <= 0:
            return []

        tasks = []
        runners: list = []
        for call_type, cls in (
            (CallType.SCOUT_C_HOW_TRUE, ScoutCHowTrueCall),
            (CallType.SCOUT_C_HOW_FALSE, ScoutCHowFalseCall),
        ):
            if await self.db.budget_remaining() <= 0:
                break
            call = await self.db.create_call(
                call_type,
                scope_page_id=target_id,
                parent_call_id=self._parent_call_id,
            )
            runner = cls(
                target_id,
                call,
                self.db,
                broadcaster=self.broadcaster,
                max_rounds=SCOUT_MAX_ROUNDS,
                fruit_threshold=SCOUT_FRUIT_THRESHOLD,
            )
            runners.append(runner)
            tasks.append(runner.run())

        if not tasks:
            return []

        results = await asyncio.gather(*tasks, return_exceptions=True)
        created: list[str] = []
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                log.warning("Critique scout failed: %s", r, exc_info=r)
                continue
            created.extend(runners[i].result.created_page_ids)
        log.info(
            "Critique scouts on %s produced %d new pages",
            target_id[:8],
            len(created),
        )
        return created

    async def _run_find_considerations(
        self,
        question_id: str,
        context_page_ids: list[str] | None,
    ) -> None:
        await find_considerations_until_done(
            question_id,
            self.db,
            parent_call_id=self._parent_call_id,
            context_page_ids=context_page_ids,
            broadcaster=self.broadcaster,
        )

    async def _run_assess(self, question_id: str) -> None:
        await assess_question(
            question_id,
            self.db,
            parent_call_id=self._parent_call_id,
            broadcaster=self.broadcaster,
            force=True,
        )

    async def _maybe_update_view(self, question_id: str, cycle: int) -> None:
        if cycle % VIEW_UPDATE_CADENCE != 0:
            return
        existing_view = await self.db.get_view_for_question(question_id)
        if existing_view:
            await update_view_for_question(
                question_id,
                self.db,
                parent_call_id=self._parent_call_id,
                broadcaster=self.broadcaster,
                force=True,
            )
        else:
            await create_view_for_question(
                question_id,
                self.db,
                parent_call_id=self._parent_call_id,
                broadcaster=self.broadcaster,
                force=True,
            )
