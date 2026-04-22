"""ClaimInvestigationOrchestrator: facade over the claim-prioritiser actor.

V2 shape — the orchestrator is a thin adapter, mirroring
``TwoPhaseOrchestrator``. It creates (or acquires) a ``ClaimPrioritiser``
for the target claim via the shared registry, wires in
DB/broadcaster/budget-cap/parent-context, transfers the budget, and
awaits completion. All round logic — phase-1 scouts, phase-2 scoring +
dispatch, recurse into child questions/claims — lives in
``ClaimPrioritiser``.

Kept for API stability: existing callers (``create_initial_call``
consumers, integration tests, older orchestrator dispatch paths) see the
same class, ``run()`` signature, and ``create_initial_call`` behaviour.

The ``from rumil.calls.prioritization import run_prioritization_call``
re-export is intentionally preserved so that tests patching at this
module's import site continue to resolve.
"""

import logging

from rumil.calls.prioritization import run_prioritization_call  # noqa: F401  (patch site)
from rumil.constants import MIN_TWOPHASE_BUDGET
from rumil.database import DB
from rumil.orchestrators.base import BaseOrchestrator
from rumil.orchestrators.common import (
    assess_question,  # noqa: F401  (patch site)
    score_items_sequentially,  # noqa: F401  (patch site)
)
from rumil.prioritisers.claim_prioritiser import ClaimPrioritiser
from rumil.tracing.broadcast import Broadcaster

log = logging.getLogger(__name__)


class ClaimInvestigationOrchestrator(BaseOrchestrator):
    summarise_before_assess: bool = True

    def __init__(
        self,
        db: DB,
        broadcaster: Broadcaster | None = None,
        budget_cap: int | None = None,
    ):
        super().__init__(db, broadcaster)
        self._budget_cap: int | None = budget_cap
        self._parent_call_id: str | None = None
        self._prio: ClaimPrioritiser | None = None

    async def _acquire_prio(self, claim_id: str) -> tuple[ClaimPrioritiser, bool]:
        registry = self.db.prioritiser_registry()
        prio, is_new = await registry.get_or_acquire(
            claim_id,
            kind="claim",
            factory=ClaimPrioritiser,
        )
        assert isinstance(prio, ClaimPrioritiser)
        prio.attach(
            self.db,
            self.broadcaster,
            budget_cap=self._budget_cap,
            parent_call_id=self._parent_call_id,
            ingest_hint=self.ingest_hint,
        )
        self._prio = prio
        return prio, is_new

    async def create_initial_call(
        self,
        claim_id: str,
        parent_call_id: str | None = None,
    ) -> str:
        """Eager-create the phase-1 PRIORITIZATION call for a claim prioritiser.

        Idempotent: repeated calls on the same prioritiser return the same id.
        """
        if parent_call_id is not None:
            self._parent_call_id = parent_call_id
        prio, _is_new = await self._acquire_prio(claim_id)
        return await prio.create_initial_call(parent_call_id=parent_call_id)

    async def run(self, root_question_id: str) -> None:
        claim_id = root_question_id
        own_db = await self.db.fork()
        self.db = own_db
        await self._setup()
        try:
            prio, is_new = await self._acquire_prio(claim_id)
            if not is_new and (prio._task is not None or prio.budget > 0):
                log.info(
                    "ClaimInvestigationOrchestrator: claim %s already has a prioritiser; awaiting completion",
                    claim_id[:8],
                )
                await prio.await_completion()
                return

            remaining = await self.db.budget_remaining()
            if self._budget_cap is not None:
                effective = min(remaining, self._budget_cap)
            else:
                effective = remaining
            if effective < MIN_TWOPHASE_BUDGET:
                await prio.mark_done()
                raise ValueError(
                    "ClaimInvestigationOrchestrator requires a budget of at least "
                    f"{MIN_TWOPHASE_BUDGET}, got {effective}"
                )

            await prio.receive_budget(effective)
            task = prio._task
            if task is not None:
                try:
                    await task
                except Exception:
                    log.exception(
                        "ClaimInvestigationOrchestrator: prioritiser task for %s failed",
                        claim_id[:8],
                    )
            if self._parent_call_id is None:
                await self.db.prioritiser_registry().teardown()
        finally:
            await self._teardown()
            await own_db.close()
