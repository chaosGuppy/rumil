"""DistillFirst-migration policies.

Port of ``DistillFirstOrchestrator`` into composable policies.

The old orchestrator implemented distill-first as a hard-coded four-step
sequence: create_view-if-missing, compute view health, pick the weakest
dimension and dispatch, then update_view. We express it as:

* ``BudgetPolicy`` (shared) — stop at zero budget.
* ``SeedViewPolicy`` — create the view when none exists.
* ``ViewHealthPolicy`` (shared) — fill missing credence, then assess
  unjudged children (mirrors the gap-picking branches).
* ``UpdateViewPolicy`` — refresh the view after any mutation.

Notes on what did NOT carry over cleanly:

* The old orchestrator's ``budget_cap`` knob (clamp budget remaining
  to a local cap) is not ported here — the factory never used it in
  practice, and ``PolicyOrchestrator`` already has ``max_iterations``
  for bounding the loop.
* The "view looks full, re-distill via update_view" fallback (the
  final branch in ``_dispatch_for_gap``) collapses into UpdateViewPolicy
  naturally: if nothing else fired and a view exists, that's the signal
  to refresh.
* The sparse-threshold check lived in ``ViewHealth.is_sparse`` and
  dispatched find_considerations. Here we rely on ``ViewHealthPolicy``
  for the gap branches and leave the sparse-question bootstrap to
  composition-level knobs (the factory can prepend a SparseQuestionPolicy
  if/when that behaviour is wanted — by default, distill_first's job
  is to surface gaps via the view, not bulk up page count).
"""

import logging
from collections.abc import Sequence

from rumil.models import CallType
from rumil.orchestrators.policy_layer import (
    BudgetPolicy,
    Intent,
    Policy,
    QuestionState,
    RunHelper,
    ViewHealthPolicy,
)

log = logging.getLogger(__name__)


_MUTATION_CALL_TYPES: frozenset[CallType] = frozenset(
    {
        CallType.FIND_CONSIDERATIONS,
        CallType.ASSESS,
        CallType.WEB_RESEARCH,
        CallType.INGEST,
        CallType.SCOUT_SUBQUESTIONS,
        CallType.SCOUT_ESTIMATES,
        CallType.SCOUT_HYPOTHESES,
        CallType.SCOUT_ANALOGIES,
        CallType.SCOUT_FACTCHECKS,
        CallType.SCOUT_WEB_QUESTIONS,
        CallType.SCOUT_DEEP_QUESTIONS,
    }
)


class SeedViewPolicy(Policy):
    """Dispatch create_view when no view exists for the question.

    This is the "distill first" half of DistillFirst: before any other
    research move, make sure a view exists so downstream policies have
    something to read gaps from. Put this early in the composition so
    the first iteration always seeds.
    """

    name = "seed_view"

    async def decide(self, state: QuestionState) -> Intent | None:
        if state.view is not None:
            return None
        log.info(
            "SeedViewPolicy: question=%s has no view, dispatching create_view",
            state.question_id[:8],
        )
        return RunHelper(
            name="create_view_for_question",
            kwargs={"question_id": state.question_id},
        )


class UpdateViewPolicy(Policy):
    """Fire update_view after a mutation call finished most recently.

    Uses ``state.recent_call_types`` (newest first) so it's stateless —
    if the most recent call on this question was a mutation (find, assess,
    ingest, scout, web_research), the next iteration refreshes the view.

    Because UPDATE_VIEW itself is not a mutation type, this policy
    naturally only fires once per mutation: after we refresh, the most
    recent call type becomes UPDATE_VIEW and the check returns None.
    """

    name = "update_view"

    async def decide(self, state: QuestionState) -> Intent | None:
        if state.view is None:
            return None
        if not state.recent_call_types:
            return None
        most_recent = state.recent_call_types[0]
        if most_recent not in _MUTATION_CALL_TYPES:
            return None
        log.info(
            "UpdateViewPolicy: last call was %s, refreshing view on %s",
            most_recent.value,
            state.question_id[:8],
        )
        return RunHelper(
            name="update_view_for_question",
            kwargs={"question_id": state.question_id},
        )


def distill_first_policies() -> Sequence[Policy]:
    """Named composition replacing DistillFirstOrchestrator.

    Priority order:
      1. BudgetPolicy — stop at zero budget.
      2. SeedViewPolicy — create view if missing.
      3. UpdateViewPolicy — refresh view after a mutation.
      4. ViewHealthPolicy — fill missing credence / unjudged children.

    UpdateView sits above ViewHealth so a freshly-dispatched assess is
    immediately followed by a view refresh (mirroring the old
    orchestrator's ``update_view_for_question`` call after every
    dispatch), and only then do we look at health again.
    """
    return [
        BudgetPolicy(),
        SeedViewPolicy(),
        UpdateViewPolicy(),
        ViewHealthPolicy(),
    ]


__all__ = [
    "SeedViewPolicy",
    "UpdateViewPolicy",
    "distill_first_policies",
]
