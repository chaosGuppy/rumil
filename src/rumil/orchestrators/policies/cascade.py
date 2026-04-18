"""Cascade-migration policies.

Port of ``CascadeOrchestrator`` into composable policies.

The old orchestrator ran ``assess_question`` against every pending
``CASCADE_REVIEW`` suggestion target until the queue drained or budget
ran out. We express that as:

* ``BudgetPolicy`` (shared) — stop at zero budget.
* ``CascadeReviewPolicy`` (shared) — pop the newest pending cascade and
  dispatch an assess on its target.
* ``NoMoreCascadesPolicy`` — terminate once no pending cascades remain,
  so the loop stops cleanly rather than spinning on an empty queue.

Notes on what did NOT carry over cleanly:

* The old orchestrator marked each processed suggestion ACCEPTED after
  its assess ran. ``CascadeReviewPolicy`` does not do that — it only
  tracks processed targets in run-local state so it does not ping-pong.
  Suggestions keep their PENDING status until some other writer updates
  them. Retained as a known delta rather than patched here: the
  reputation dashboard consumes suggestion status and any fix belongs in
  ``CascadeReviewPolicy`` (shared) rather than in the migration layer.
* The old orchestrator's stale-suggestion dismissal (``_is_stale``) is
  likewise not replicated — same reasoning: that's policy-level logic
  that should live in ``CascadeReviewPolicy`` if we want it back.
"""

import logging
from collections.abc import Sequence

from rumil.database import DB
from rumil.models import SuggestionType
from rumil.orchestrators.policy_layer import (
    BudgetPolicy,
    CascadeReviewPolicy,
    Intent,
    Policy,
    QuestionState,
    Terminate,
)

log = logging.getLogger(__name__)


class NoMoreCascadesPolicy(Policy):
    """Terminate when no pending CASCADE_REVIEW suggestions remain.

    The CascadeOrchestrator's whole job is draining the cascade queue.
    Without this policy the loop would keep re-checking an empty queue
    until ``max_iterations`` — wasteful. Place this after
    ``CascadeReviewPolicy`` so we only terminate once that policy has had
    a chance to pick up any pending work.
    """

    name = "no_more_cascades"

    def __init__(self, db: DB) -> None:
        self._db = db

    async def decide(self, state: QuestionState) -> Intent | None:
        pending = await self._db.get_pending_suggestions()
        has_cascade = any(s.suggestion_type == SuggestionType.CASCADE_REVIEW for s in pending)
        if has_cascade:
            return None
        return Terminate(reason="no pending cascade_review suggestions")


def cascade_policies(db: DB) -> Sequence[Policy]:
    """Named composition replacing CascadeOrchestrator.

    Priority order:
      1. BudgetPolicy — stop at zero budget.
      2. CascadeReviewPolicy — dispatch assess on the newest pending cascade.
      3. NoMoreCascadesPolicy — terminate cleanly when the queue is empty.
    """
    return [
        BudgetPolicy(),
        CascadeReviewPolicy(db),
        NoMoreCascadesPolicy(db),
    ]


__all__ = [
    "NoMoreCascadesPolicy",
    "cascade_policies",
]
