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
from rumil.models import CallType, Page, Suggestion, SuggestionStatus, SuggestionType
from rumil.orchestrators.base import BaseOrchestrator
from rumil.orchestrators.common import (
    assess_question,
    check_triage_before_run,
    create_view_for_question,
    find_considerations_until_done,
    update_view_for_question,
    web_research_question,
)
from rumil.tensions import TENSION_CREDENCE_THRESHOLD, unexplored_tension_candidates
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

    consideration_page_ids: Sequence[str] = field(default_factory=tuple)
    child_question_ids: Sequence[str] = field(default_factory=tuple)

    @property
    def page_count(self) -> int:
        return self.consideration_count + self.child_question_count

    @property
    def view_scope_page_ids(self) -> frozenset[str]:
        """Page IDs that belong to this question's view scope.

        Question itself + considerations + child questions. Used by
        policies that need to match pending suggestions (e.g.
        cascade-review targets) against the current research subtree.
        """
        return frozenset((self.question_id, *self.consideration_page_ids, *self.child_question_ids))

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
            consideration_page_ids=tuple(p.id for p in consideration_pages),
            child_question_ids=tuple(child_ids),
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


class CascadeReviewPolicy(Policy):
    """Dispatch assess when a pending CASCADE_REVIEW suggestion exists.

    The substrate side: ``check_cascades`` emits ``CASCADE_REVIEW``
    suggestions whenever a page's credence / robustness / importance
    crosses its threshold. This policy is the consumer: on each tick it
    pops the newest pending cascade and asks the orchestrator to assess
    the target page.

    Compose-friendly: put it high in the priority list (e.g. after
    BudgetPolicy but before SparseQuestionPolicy / ViewHealthPolicy) to
    let fresh reputation-signal propagation pre-empt normal research
    cadence. Returns None when no pending cascade exists, so downstream
    policies run as usual.

    QuestionState is a pure snapshot and doesn't carry a DB handle, so
    this policy accepts the DB in its constructor. Policies that don't
    need DB access stay ergonomic; ones that do (like this one) opt in.
    """

    name = "cascade_review"

    def __init__(self, db: DB) -> None:
        self._db = db
        self._processed_targets: set[str] = set()

    async def decide(self, state: QuestionState) -> Intent | None:
        pending = await self._db.get_pending_suggestions()
        cascade_pending = [s for s in pending if s.suggestion_type == SuggestionType.CASCADE_REVIEW]
        if not cascade_pending:
            return None
        cascade_pending.sort(key=lambda s: s.created_at, reverse=True)
        for s in cascade_pending:
            if s.target_page_id in self._processed_targets:
                continue
            self._processed_targets.add(s.target_page_id)
            return DispatchCall(
                call_type=CallType.ASSESS,
                kwargs={"question_id": s.target_page_id},
            )
        return None


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


class TensionExplorationPolicy(Policy):
    """Dispatch an ExploreTensionCall against the highest-confidence unexplored tension.

    Owen-bolded priority: track + explore tensions. On each iteration we
    scan the root question's considerations for pairs of high-credence
    claims whose directions conflict on the question (cheap structural
    scan; see ``rumil.tensions``). For the top unexplored candidate we
    emit a ``DispatchCall(CallType.EXPLORE_TENSION)`` with the tension
    triple in ``kwargs``.

    ``emit_suggestion=True`` also writes a RESOLVE_TENSION suggestion to
    the suggestions table so the reputation dashboard / UI can surface
    the tension to the user even before the explorer runs.

    Returns None when the question has no unexplored tensions — lets
    downstream policies take over.
    """

    name = "tension_exploration"

    def __init__(
        self,
        *,
        credence_threshold: int = TENSION_CREDENCE_THRESHOLD,
        include_semantic: bool = False,
        emit_suggestion: bool = True,
    ) -> None:
        self._credence_threshold = credence_threshold
        self._include_semantic = include_semantic
        self._emit_suggestion = emit_suggestion
        self._db: DB | None = None

    def bind_db(self, db: DB) -> None:
        """Attach the DB used for the workspace-read scan.

        Exposed as a separate step because ``Policy.decide`` takes only
        state; the scan needs DB access. ``PolicyOrchestrator`` calls this
        once at run setup — bespoke callers must bind explicitly.
        """
        self._db = db

    async def decide(self, state: QuestionState) -> Intent | None:
        if self._db is None:
            log.debug("TensionExplorationPolicy: no DB bound, skipping")
            return None

        candidates = await unexplored_tension_candidates(
            self._db,
            state.question_id,
            credence_threshold=self._credence_threshold,
            include_semantic=self._include_semantic,
        )
        if not candidates:
            return None

        top = max(candidates, key=lambda c: c.confidence)

        if self._emit_suggestion:
            await self._emit_suggestion_for(top)

        return DispatchCall(
            call_type=CallType.EXPLORE_TENSION,
            kwargs={
                "tension_question_id": top.question_id,
                "tension_claim_a_id": top.claim_a_id,
                "tension_claim_b_id": top.claim_b_id,
                "tension_kind": top.kind,
                "tension_reason": top.reason,
            },
        )

    async def _emit_suggestion_for(self, candidate) -> None:
        assert self._db is not None
        suggestion = Suggestion(
            project_id=self._db.project_id,
            workspace="research",
            run_id=self._db.run_id,
            suggestion_type=SuggestionType.RESOLVE_TENSION,
            target_page_id=candidate.claim_a_id,
            source_page_id=candidate.claim_b_id,
            status=SuggestionStatus.PENDING,
            payload={
                "question_id": candidate.question_id,
                "claim_a_id": candidate.claim_a_id,
                "claim_b_id": candidate.claim_b_id,
                "other_node_id": candidate.claim_b_id,
                "kind": candidate.kind,
                "reason": candidate.reason,
                "confidence": candidate.confidence,
                "reasoning": candidate.reason,
            },
            staged=self._db.staged,
        )
        try:
            await self._db.save_suggestion(suggestion)
        except Exception:
            log.debug("TensionExplorationPolicy: save_suggestion failed", exc_info=True)


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
        for policy in self._policies:
            binder = getattr(policy, "bind_db", None)
            if callable(binder):
                binder(db)

    async def run(self, root_question_id: str) -> None:
        if not await check_triage_before_run(self.db, root_question_id):
            return
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
        elif intent.call_type == CallType.EXPLORE_TENSION:
            await _explore_tension_dispatch(
                db=self.db,
                question_id=kwargs.get("question_id", scope_question_id),
                parent_call_id=kwargs.get("parent_call_id"),
                broadcaster=kwargs.get("broadcaster"),
                tension_question_id=kwargs["tension_question_id"],
                tension_claim_a_id=kwargs["tension_claim_a_id"],
                tension_claim_b_id=kwargs["tension_claim_b_id"],
                tension_kind=kwargs.get("tension_kind", "direction_conflict"),
                tension_reason=kwargs.get("tension_reason", ""),
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


async def _explore_tension_dispatch(
    *,
    db: DB,
    question_id: str,
    parent_call_id: str | None,
    broadcaster: Broadcaster | None,
    tension_question_id: str,
    tension_claim_a_id: str,
    tension_claim_b_id: str,
    tension_kind: str,
    tension_reason: str,
) -> str:
    """Create + run a single ExploreTensionCall. Returns the call id.

    Kept at module scope (not a class method) so tests can patch it cleanly
    via ``mocker.patch("rumil.orchestrators.policy_layer._explore_tension_dispatch")``.
    """
    from rumil.calls.explore_tension import ExploreTensionCall

    call = await db.create_call(
        CallType.EXPLORE_TENSION,
        scope_page_id=tension_question_id,
        parent_call_id=parent_call_id,
    )
    call.call_params = {
        **(call.call_params or {}),
        "tension_question_id": tension_question_id,
        "tension_claim_a_id": tension_claim_a_id,
        "tension_claim_b_id": tension_claim_b_id,
        "tension_kind": tension_kind,
        "tension_reason": tension_reason,
    }
    runner = ExploreTensionCall(
        tension_question_id,
        call,
        db,
        broadcaster=broadcaster,
    )
    await runner.run()
    return call.id


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
    "CascadeReviewPolicy",
    "DispatchCall",
    "Intent",
    "Policy",
    "PolicyOrchestrator",
    "QuestionState",
    "RunHelper",
    "SparseQuestionPolicy",
    "TensionExplorationPolicy",
    "Terminate",
    "ViewHealthPolicy",
    "two_phase_like_policies",
]
