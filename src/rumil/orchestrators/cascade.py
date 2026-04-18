"""CascadeOrchestrator: consume pending CASCADE_REVIEW suggestions.

Design
------
The marketplace-thread "common primitives" doc called out cascading
reassessment as a key consumer of the reputation / suggestion substrate
we already ship. `check_cascades` (see `rumil.cascades`) emits
``Suggestion`` rows of type ``CASCADE_REVIEW`` whenever a page's
credence / robustness / importance crosses its per-field threshold. Those
suggestions target every page that DEPENDS_ON the changed page — i.e.
every higher-level claim whose justification might now be stale.

This orchestrator is the consumer side. It repeatedly:

1. Reads pending ``CASCADE_REVIEW`` suggestions for the current project.
2. Filters out stale ones — a suggestion whose target has already been
   reassessed since the suggestion was created is treated as resolved
   (the downstream assess either already saw the upstream change or was
   independent enough not to need to).
3. Picks the highest-priority remaining suggestion (newest first is fine
   for now; priority signals can be layered in later).
4. Runs ``assess_question`` against the target page. ``assess_question``
   itself consumes one unit of budget.
5. Marks the suggestion ACCEPTED after the assess runs.
6. Loops until no pending cascade suggestions remain OR budget is
   exhausted.

The orchestrator does NOT write epistemic scores directly — cascading
TRIGGERS an assess, the assess call is what changes scores (and may in
turn enqueue further cascade suggestions up the dependency chain).

Notes on root_question_id
-------------------------
The base ``run(root_question_id)`` signature is question-scoped. For
cascades we don't actually need a root question — cascade suggestions
are scoped by project, not by question subtree. We accept the argument
to honour the ABC, but primarily use it as a starting-context hint for
logging / tracing. A future refactor might introduce a root-optional
run signature in ``BaseOrchestrator``.
"""

import logging
from collections.abc import Sequence
from datetime import datetime

from rumil.database import DB
from rumil.models import Suggestion, SuggestionStatus, SuggestionType
from rumil.orchestrators.base import BaseOrchestrator
from rumil.orchestrators.common import assess_question
from rumil.tracing.broadcast import Broadcaster

log = logging.getLogger(__name__)

MAX_ITERATIONS = 100


class CascadeOrchestrator(BaseOrchestrator):
    """Consume pending CASCADE_REVIEW suggestions, one assess per iteration.

    See module docstring for design. The loop is deliberately boring:
    pick a suggestion, assess its target, mark resolved, repeat.
    """

    def __init__(
        self,
        db: DB,
        broadcaster: Broadcaster | None = None,
        max_iterations: int = MAX_ITERATIONS,
    ):
        super().__init__(db, broadcaster)
        self._parent_call_id: str | None = None
        self._max_iterations = max_iterations

    async def run(self, root_question_id: str) -> None:
        await self._setup()
        try:
            await self._loop(root_question_id)
        finally:
            await self._teardown()

    async def _loop(self, root_question_id: str) -> None:
        processed_targets: set[str] = set()
        for iteration in range(1, self._max_iterations + 1):
            remaining = await self.db.budget_remaining()
            if remaining <= 0:
                log.info(
                    "CascadeOrchestrator: budget exhausted at iteration %d",
                    iteration,
                )
                break

            suggestion = await self._pick_next(processed_targets)
            if suggestion is None:
                log.info(
                    "CascadeOrchestrator: no pending cascade suggestions, stopping (iteration=%d)",
                    iteration,
                )
                break

            log.info(
                "CascadeOrchestrator iteration %d: cascading %s -> target=%s",
                iteration,
                (suggestion.source_page_id or "?")[:8],
                suggestion.target_page_id[:8],
            )

            call_id = await assess_question(
                suggestion.target_page_id,
                self.db,
                parent_call_id=self._parent_call_id,
                broadcaster=self.broadcaster,
                force=False,
            )

            if call_id is None:
                log.info(
                    "CascadeOrchestrator: budget ran out during assess; leaving "
                    "suggestion %s pending",
                    suggestion.id[:8],
                )
                break

            await self.db.update_suggestion_status(
                suggestion.id,
                SuggestionStatus.ACCEPTED,
            )
            processed_targets.add(suggestion.target_page_id)

        log.info(
            "CascadeOrchestrator complete: question=%s, processed=%d",
            root_question_id[:8],
            len(processed_targets),
        )

    async def _pick_next(self, already_processed: set[str]) -> Suggestion | None:
        """Return the next actionable cascade suggestion, or None.

        Filters out:
          * targets already handled this run (prevents ping-pong within a
            single CascadeOrchestrator run if a downstream assess flips
            the same upstream back into cascade territory).
          * stale suggestions (target has been reassessed since the
            suggestion was created — see ``_is_stale``).
        """
        pending = await self.db.get_pending_suggestions()
        cascade_pending = [s for s in pending if s.suggestion_type == SuggestionType.CASCADE_REVIEW]
        if not cascade_pending:
            return None

        cascade_pending.sort(key=lambda s: s.created_at, reverse=True)

        for s in cascade_pending:
            if s.target_page_id in already_processed:
                continue
            if await self._is_stale(s):
                log.info(
                    "CascadeOrchestrator: marking stale suggestion %s as dismissed "
                    "(target %s reassessed since %s)",
                    s.id[:8],
                    s.target_page_id[:8],
                    s.created_at.isoformat(),
                )
                await self.db.update_suggestion_status(s.id, SuggestionStatus.DISMISSED)
                continue
            return s
        return None

    async def _is_stale(self, suggestion: Suggestion) -> bool:
        """A cascade suggestion is stale if its target has been reassessed
        after the suggestion was created — i.e. a later epistemic_scores
        row exists for ``target_page_id`` with ``created_at`` strictly
        after ``suggestion.created_at``.
        """
        score_row, _ = await self.db.get_epistemic_score_source(suggestion.target_page_id)
        if score_row is None:
            return False
        created_at_raw = score_row.get("created_at")
        if created_at_raw is None:
            return False
        if isinstance(created_at_raw, str):
            try:
                score_dt = datetime.fromisoformat(created_at_raw)
            except ValueError:
                return False
        elif isinstance(created_at_raw, datetime):
            score_dt = created_at_raw
        else:
            return False
        return score_dt > suggestion.created_at


def pending_cascade_suggestions(
    suggestions: Sequence[Suggestion],
) -> Sequence[Suggestion]:
    """Filter a suggestions list down to pending CASCADE_REVIEW rows.

    Shared helper for the orchestrator and the policy-layer policy so
    they stay in sync on the shape of "pending cascade".
    """
    return [
        s
        for s in suggestions
        if s.suggestion_type == SuggestionType.CASCADE_REVIEW
        and s.status == SuggestionStatus.PENDING
    ]


__all__ = ["CascadeOrchestrator", "pending_cascade_suggestions"]
