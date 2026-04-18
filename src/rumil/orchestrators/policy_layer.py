"""Dispatch-policy layer — scaffolding for composable orchestrator policies.

See marketplace-thread/31-dispatch-policy-layer.md for the design doc.

A policy takes a QuestionState and returns an Intent (or None if it has
nothing to say). A PolicyOrchestrator composes a list of policies and
runs a priority-list loop: first non-None intent wins per iteration.

This module is scaffolding. It demonstrates the abstraction with three
concrete primitives and a named composition. It does NOT migrate any of
the existing orchestrators.
"""

import logging
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from rumil.database import DB
from rumil.models import CallType, Page
from rumil.orchestrators.base import BaseOrchestrator
from rumil.orchestrators.common import (
    assess_question,
    create_view_for_question,
    find_considerations_until_done,
    update_view_for_question,
    web_research_question,
)
from rumil.tracing.broadcast import Broadcaster

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class QuestionState:
    """A snapshot of the world relevant to policy decisions.

    Populated once per orchestrator iteration via ``capture`` and passed to
    every policy in the composition. Per-run / per-policy history does NOT
    live here — that's policy-local state. This is a snapshot of the
    workspace, not of the run.
    """

    question_id: str
    budget_remaining: int
    iteration: int

    consideration_count: int
    child_question_count: int
    source_count: int

    view: Page | None
    missing_credence_page_ids: Sequence[str]
    missing_importance_item_ids: Sequence[str]
    unjudged_child_question_ids: Sequence[str]

    recent_call_types: Sequence[CallType]

    @property
    def page_count(self) -> int:
        return self.consideration_count + self.child_question_count

    @classmethod
    async def capture(
        cls,
        db: DB,
        question_id: str,
        *,
        iteration: int,
        recent_calls_limit: int = 5,
    ) -> "QuestionState":
        """Populate QuestionState from DB reads.

        Prefers existing batched helpers. The goal is one populate per
        iteration, passed to every policy — cheaper than today's situation
        where each orchestrator re-reads parts of this state multiple times.
        """
        budget_remaining = await db.budget_remaining()

        considerations = await db.get_considerations_for_question(question_id)
        consideration_pages = [p for p, _ in considerations]
        child_questions = await db.get_child_questions(question_id)

        view = await db.get_view_for_question(question_id)

        missing_credence = [p.id for p in consideration_pages if p.credence is None]

        missing_importance: list[str] = []
        if view is not None:
            view_items_with_links = await db.get_view_items(view.id)
            missing_importance = [
                p.id for p, link in view_items_with_links if link.importance is None
            ]

        child_ids = [c.id for c in child_questions]
        judgements_by_q = await db.get_judgements_for_questions(child_ids) if child_ids else {}
        unjudged_children = [cid for cid in child_ids if not judgements_by_q.get(cid)]

        source_count = 0
        try:
            from rumil.orchestrators.common import count_sources_for_question

            source_count = await count_sources_for_question(db, question_id)
        except Exception:
            log.debug(
                "QuestionState.capture: count_sources_for_question failed for %s",
                question_id[:8],
                exc_info=True,
            )

        recent_call_types = await _recent_call_types(db, question_id, recent_calls_limit)

        return cls(
            question_id=question_id,
            budget_remaining=budget_remaining,
            iteration=iteration,
            consideration_count=len(consideration_pages),
            child_question_count=len(child_questions),
            source_count=source_count,
            view=view,
            missing_credence_page_ids=missing_credence,
            missing_importance_item_ids=missing_importance,
            unjudged_child_question_ids=unjudged_children,
            recent_call_types=recent_call_types,
        )


async def _recent_call_types(db: DB, question_id: str, limit: int) -> Sequence[CallType]:
    """Best-effort recent-call-type lookup, newest first.

    Defensive: if the DB lacks the helper (older branches, test mocks that
    only cover the fields this policy actually reads), returns []. Callers
    should check emptiness rather than failing.
    """
    getter = getattr(db, "get_recent_calls_for_question", None)
    if getter is None:
        return []
    try:
        calls = await getter(question_id, limit=limit)
    except Exception:
        log.debug("recent call types lookup failed", exc_info=True)
        return []
    result: list[CallType] = []
    for c in calls:
        try:
            result.append(CallType(c.call_type))
        except ValueError:
            continue
    return result


@dataclass(frozen=True)
class DispatchCall:
    """Intent: dispatch a single call type (find_considerations, assess, etc)."""

    call_type: CallType
    kwargs: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RunHelper:
    """Intent: run one of the multi-step helpers in orchestrators.common.

    Escape hatch for helpers that aren't cleanly single-call dispatches
    (``create_view_for_question``, ``update_view_for_question``,
    ``find_considerations_until_done``, ``ingest_until_done``). The name
    must appear in PolicyOrchestrator._HELPERS.
    """

    name: str
    kwargs: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Terminate:
    """Intent: stop the loop. ``reason`` is shown in logs / trace."""

    reason: str


Intent = DispatchCall | RunHelper | Terminate


class Policy(ABC):
    """A policy is a function from (state) -> Intent | None.

    Returning None means "this policy has nothing to say about this state"
    and lets composition fall through to the next policy. Stateful policies
    (e.g., "scouts produced nothing two rounds in a row") store their own
    run-local state as instance attributes — QuestionState is world state,
    not run state.
    """

    name: str = ""

    @abstractmethod
    async def decide(self, state: QuestionState) -> Intent | None: ...


class BudgetPolicy(Policy):
    """Terminate when budget hits zero. Put this first in the composition."""

    name = "budget"

    async def decide(self, state: QuestionState) -> Intent | None:
        if state.budget_remaining <= 0:
            return Terminate(reason="budget_exhausted")
        return None


class SparseQuestionPolicy(Policy):
    """Dispatch find_considerations while page count is below threshold.

    "Page count" here means considerations + child questions — matches
    DistillFirstOrchestrator's existing is_sparse check.
    """

    name = "sparse_question"

    def __init__(self, threshold: int = 3):
        self.threshold = threshold

    async def decide(self, state: QuestionState) -> Intent | None:
        if state.page_count >= self.threshold:
            return None
        return RunHelper(
            name="find_considerations_until_done",
            kwargs={"question_id": state.question_id},
        )


class ViewHealthPolicy(Policy):
    """Dispatch assess to fill missing credence/importance scores.

    Fires on missing_credence_page_ids first, then on
    unjudged_child_question_ids. If a view exists with missing importance
    but no other gaps, yields to downstream policies — missing importance
    is fixed by update_view, not assess, and that belongs on a separate
    policy (not in scope here).
    """

    name = "view_health"

    async def decide(self, state: QuestionState) -> Intent | None:
        if state.missing_credence_page_ids:
            target = state.missing_credence_page_ids[0]
            return DispatchCall(
                call_type=CallType.ASSESS,
                kwargs={"question_id": target},
            )
        if state.unjudged_child_question_ids:
            target = state.unjudged_child_question_ids[0]
            return DispatchCall(
                call_type=CallType.ASSESS,
                kwargs={"question_id": target},
            )
        return None


class PolicyOrchestrator(BaseOrchestrator):
    """Boring loop: capture state, ask policies in order, execute intent.

    The interesting logic lives in the policies. This class owns only:
      * the iteration loop
      * state capture
      * intent dispatch (the handler table below)
    """

    _HELPER_NAMES: frozenset[str] = frozenset(
        {
            "find_considerations_until_done",
            "create_view_for_question",
            "update_view_for_question",
        }
    )

    def __init__(
        self,
        db: DB,
        policies: Sequence[Policy],
        broadcaster: Broadcaster | None = None,
        max_iterations: int = 50,
    ):
        super().__init__(db, broadcaster)
        self._policies = list(policies)
        self._max_iterations = max_iterations
        self._parent_call_id: str | None = None

    async def run(self, root_question_id: str) -> None:
        await self._setup()
        try:
            iteration = 0
            while iteration < self._max_iterations:
                state = await QuestionState.capture(
                    self.db,
                    root_question_id,
                    iteration=iteration,
                )
                intent = await self._pick(state)
                if intent is None:
                    log.info(
                        "PolicyOrchestrator: no policy matched, stopping (iteration=%d)",
                        iteration,
                    )
                    break
                if isinstance(intent, Terminate):
                    log.info(
                        "PolicyOrchestrator: terminating — %s (iteration=%d)",
                        intent.reason,
                        iteration,
                    )
                    break
                await self._execute(intent, root_question_id)
                iteration += 1
        finally:
            await self._teardown()

    async def _pick(self, state: QuestionState) -> Intent | None:
        """First-non-None wins. Each policy sees the same snapshot."""
        for p in self._policies:
            intent = await p.decide(state)
            if intent is not None:
                log.info(
                    "PolicyOrchestrator iteration=%d: policy=%s picked %s",
                    state.iteration,
                    p.name or type(p).__name__,
                    type(intent).__name__,
                )
                return intent
        return None

    async def _execute(self, intent: Intent, scope_question_id: str) -> None:
        """Dispatch the chosen intent. Terminate is never routed here."""
        if isinstance(intent, DispatchCall):
            await self._execute_dispatch_call(intent, scope_question_id)
        elif isinstance(intent, RunHelper):
            await self._execute_helper(intent)
        else:
            raise TypeError(f"Unhandled intent type: {type(intent).__name__}")

    async def _execute_dispatch_call(
        self,
        intent: DispatchCall,
        scope_question_id: str,
    ) -> None:
        """Route a single-call intent to the matching common.py helper.

        Scaffolding maps the three call types that the primitive policies
        can emit: ASSESS, FIND_CONSIDERATIONS, WEB_RESEARCH. Extending this
        is mechanical — add a branch plus the import.
        """
        kwargs = dict(intent.kwargs)
        kwargs.setdefault("question_id", scope_question_id)
        kwargs.setdefault("parent_call_id", self._parent_call_id)
        kwargs.setdefault("broadcaster", self.broadcaster)

        if intent.call_type == CallType.ASSESS:
            await assess_question(
                db=self.db,
                **kwargs,
            )
        elif intent.call_type == CallType.FIND_CONSIDERATIONS:
            await find_considerations_until_done(
                db=self.db,
                **kwargs,
            )
        elif intent.call_type == CallType.WEB_RESEARCH:
            await web_research_question(
                db=self.db,
                **kwargs,
            )
        else:
            raise NotImplementedError(
                f"DispatchCall for {intent.call_type.value} not wired in "
                "PolicyOrchestrator scaffolding. Extend _execute_dispatch_call."
            )

    async def _execute_helper(self, intent: RunHelper) -> None:
        """Route a RunHelper intent to the named helper.

        Looked up through the module globals (not a class-level dict) so
        that tests patching ``rumil.orchestrators.policy_layer.<helper>``
        actually reach the dispatched call.
        """
        if intent.name not in self._HELPER_NAMES:
            raise NotImplementedError(
                f"RunHelper name {intent.name!r} not registered. "
                f"Known: {sorted(self._HELPER_NAMES)}"
            )
        helper = globals()[intent.name]
        kwargs = dict(intent.kwargs)
        kwargs.setdefault("parent_call_id", self._parent_call_id)
        kwargs.setdefault("broadcaster", self.broadcaster)
        await helper(db=self.db, **kwargs)


def two_phase_like_policies() -> Sequence[Policy]:
    """Named composition: a tiny slice of TwoPhaseOrchestrator's bootstrap.

    This is not a faithful port of TwoPhaseOrchestrator — that one uses an
    LLM-driven prioritization call that emits batched dispatches, which
    the scaffolding doesn't yet handle (see §6 of the design doc).

    What this does reproduce: the sparse-question bootstrap behavior.
    When a new question has no considerations yet, TwoPhase's initial
    prioritization fans out scouts; here we emit one find_considerations
    pass (the simplest proxy). Once page_count clears the threshold,
    ViewHealthPolicy handles assess on any unjudged children /
    missing-credence pages. BudgetPolicy stops the loop.

    The composition demonstrates: priority ordering (budget first),
    fall-through (sparse returns None once threshold cleared), and
    terminate-as-intent (budget turning the loop off).
    """
    return [
        BudgetPolicy(),
        SparseQuestionPolicy(threshold=3),
        ViewHealthPolicy(),
    ]


__all__ = [
    "BudgetPolicy",
    "DispatchCall",
    "Intent",
    "Policy",
    "PolicyOrchestrator",
    "QuestionState",
    "RunHelper",
    "SparseQuestionPolicy",
    "Terminate",
    "ViewHealthPolicy",
    "two_phase_like_policies",
]
