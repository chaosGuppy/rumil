"""ClaimPrioritiser: Prioritiser subclass for claim-kind investigations.

V1 scaffolding. ``ClaimInvestigationOrchestrator`` still drives its own
round loop; this subclass is the V2 landing spot. It overrides
``_fire_subscription`` to deliver via ``assess_question`` (matching the
orchestrator's deliverable shape).
"""

import logging
from typing import TYPE_CHECKING

from rumil.prioritisers.prioritiser import Prioritiser
from rumil.prioritisers.subscription import Subscription

if TYPE_CHECKING:
    from rumil.database import DB
    from rumil.tracing.broadcast import Broadcaster

log = logging.getLogger(__name__)


class ClaimPrioritiser(Prioritiser):
    def __init__(self, question_id: str, kind: str = "claim") -> None:
        super().__init__(question_id, kind=kind)
        self.db: DB | None = None
        self.broadcaster: Broadcaster | None = None
        self._budget_cap: int | None = None
        self._parent_call_id: str | None = None
        self.ingest_hint: str = ""

    def attach(
        self,
        db: "DB",
        broadcaster: "Broadcaster | None" = None,
        *,
        budget_cap: int | None = None,
        parent_call_id: str | None = None,
        ingest_hint: str = "",
    ) -> None:
        """First-parent-wins attach of DB + broadcaster + context.

        Mirrors ``QuestionPrioritiser.attach`` so the shared recurse
        dispatch path can pass the same kwargs to either kind.
        """
        if self.db is None:
            self.db = db
        if self.broadcaster is None and broadcaster is not None:
            self.broadcaster = broadcaster
        if self._budget_cap is None and budget_cap is not None:
            self._budget_cap = budget_cap
        if self._parent_call_id is None and parent_call_id is not None:
            self._parent_call_id = parent_call_id
        if ingest_hint and not self.ingest_hint:
            self.ingest_hint = ingest_hint

    async def _fire_subscription(self, subscription: Subscription) -> None:
        if self._last_delivered_call_id is not None:
            subscription.resolve(self._last_delivered_call_id)
            return
        if self.db is None:
            subscription.resolve(None)
            return
        from rumil.orchestrators.common import assess_question

        try:
            call_id = await assess_question(
                self.question_id,
                self.db,
                broadcaster=self.broadcaster,
                force=True,
            )
        except Exception:
            log.exception(
                "ClaimPrioritiser %s failed to produce force-fire deliverable",
                self.question_id[:8],
            )
            call_id = None
        self._last_delivered_call_id = call_id
        subscription.resolve(call_id)
