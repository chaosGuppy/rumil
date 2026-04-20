"""Worldview-migration policies.

Port of ``WorldviewOrchestrator._decide_mode`` into composable policies.

The old orchestrator cycled between "explore" and "evaluate" modes by
inspecting view health + pending cascade suggestions + the assess/explore
call-count ratio. We express that as:

* ``BudgetPolicy`` (shared) — stop at zero budget.
* ``ViewHealthPolicy`` (shared) — fill missing credence/importance.
* ``EvaluateModePolicy`` — dispatch assess when a pending CASCADE_REVIEW
  suggestion targets a page in this question's view scope. Mirrors the
  old orchestrator's cascade-driven evaluate branch.
* ``ExploreModePolicy`` — dispatch find_considerations when considerations
  are sparse AND no pending cascade suggestion is waiting. Mirrors the
  old orchestrator's explore branch (the "too few pages" fallback).

Notes on what did NOT carry over cleanly:

* The old "last-explored > assess" ratio heuristic (rule 4 in
  ``_decide_mode``) is dropped. It was a cadence hack that smoothed
  alternation when nothing else was true; with the explicit priority
  ordering (evaluate-cascades > fill-gaps > explore-sparse) the same
  behaviour emerges without the counter.
* The old "no judgement + >=5 considerations" rule (rule 3) is also
  dropped. ``ViewHealthPolicy`` already covers the unjudged-children
  case, and once credence/importance gaps close we prefer to keep
  exploring until a real cascade or view-health signal fires — rather
  than mechanically re-assessing.
"""

import logging
from collections.abc import Sequence

from rumil.database import DB
from rumil.models import CallType, SuggestionType
from rumil.orchestrators.policy_layer import (
    BudgetPolicy,
    DispatchCall,
    Intent,
    Policy,
    QuestionState,
    RunHelper,
    ViewHealthPolicy,
)

log = logging.getLogger(__name__)


EXPLORE_SPARSE_THRESHOLD = 3


class EvaluateModePolicy(Policy):
    """Dispatch assess when a pending cascade review targets this question's scope.

    Mirrors ``WorldviewOrchestrator._decide_mode``'s evaluate branch:
    cascade suggestions propagate credence-rating updates upward, and
    the evaluate mode reacted by re-assessing the target. We scope the
    match to ``state.view_scope_page_ids`` (question + considerations +
    children) so a cascade fired by a page in an unrelated question
    doesn't pull focus.

    Tracks processed targets on the instance so we don't loop on the
    same suggestion once it's been dispatched.
    """

    name = "evaluate_mode"
    description = (
        "When a pending CASCADE_REVIEW targets a page inside this "
        "question's view scope, dispatch an assess on that target."
    )

    def __init__(self, db: DB) -> None:
        self._db = db
        self._processed_targets: set[str] = set()

    async def decide(self, state: QuestionState) -> Intent | None:
        pending = await self._db.get_pending_suggestions()
        scope = state.view_scope_page_ids
        in_scope = [
            s
            for s in pending
            if s.suggestion_type == SuggestionType.CASCADE_REVIEW and s.target_page_id in scope
        ]
        if not in_scope:
            return None
        in_scope.sort(key=lambda s: s.created_at, reverse=True)
        for s in in_scope:
            if s.target_page_id in self._processed_targets:
                continue
            self._processed_targets.add(s.target_page_id)
            log.info(
                "EvaluateModePolicy: cascade in scope, dispatching assess on %s",
                s.target_page_id[:8],
            )
            return DispatchCall(
                call_type=CallType.ASSESS,
                kwargs={"question_id": s.target_page_id},
            )
        return None


class ExploreModePolicy(Policy):
    """Dispatch find_considerations when considerations are sparse and nothing evaluate-worthy is pending.

    Mirrors ``WorldviewOrchestrator._decide_mode``'s explore branch. The
    old orchestrator fired explore when ``view.health.total_pages < 3``;
    here "page count" is considerations + child questions, same metric.

    Gated on "no pending CASCADE_REVIEW in scope" so this policy yields
    to ``EvaluateModePolicy`` when they'd both fire. That matches the
    old orchestrator's priority ordering (cascade-check comes before
    the sparse fallback).
    """

    name = "explore_mode"
    description = (
        "Sparse fallback: if page count is under threshold and no "
        "in-scope cascade is pending, dispatch find_considerations."
    )

    def __init__(self, db: DB, threshold: int = EXPLORE_SPARSE_THRESHOLD) -> None:
        self._db = db
        self._threshold = threshold

    async def decide(self, state: QuestionState) -> Intent | None:
        if state.page_count >= self._threshold:
            return None
        pending = await self._db.get_pending_suggestions()
        scope = state.view_scope_page_ids
        cascade_in_scope = any(
            s.suggestion_type == SuggestionType.CASCADE_REVIEW and s.target_page_id in scope
            for s in pending
        )
        if cascade_in_scope:
            return None
        log.info(
            "ExploreModePolicy: question=%s sparse (pages=%d), dispatching find_considerations",
            state.question_id[:8],
            state.page_count,
        )
        return RunHelper(
            name="find_considerations_until_done",
            kwargs={"question_id": state.question_id},
        )


def worldview_policies(db: DB) -> Sequence[Policy]:
    """Named composition replacing WorldviewOrchestrator.

    Priority order:
      1. BudgetPolicy — stop at zero budget.
      2. EvaluateModePolicy — react to cascades in scope.
      3. ViewHealthPolicy — fill missing credence/importance.
      4. ExploreModePolicy — sparse fallback: find_considerations.

    If none of the above fires (view is full, no cascades, above
    sparse threshold), the loop stops — matching the old
    orchestrator's "default to explore" behavior in the absence of
    signals would keep looping forever on a saturated question; the
    policy version prefers to stop and let the caller decide.
    """
    return [
        BudgetPolicy(),
        EvaluateModePolicy(db),
        ViewHealthPolicy(),
        ExploreModePolicy(db),
    ]


__all__ = [
    "EXPLORE_SPARSE_THRESHOLD",
    "EvaluateModePolicy",
    "ExploreModePolicy",
    "worldview_policies",
]
