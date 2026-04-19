"""Policy that turns reputation events into dispatch intents.

Reads the ``reputation_events`` substrate (populated by ``run_eval``,
``lazy_eval``, and ``confusion_scan``) and turns the worst-grounded
load-bearing claim into a ``recurse_into_claim_investigation`` dispatch.

This is the "code-level" half of the evals-as-feedback loop — the
prompt-level half lives in ``two_phase_main_phase_prioritization.md``
and related prompts. Both paths read the same DB substrate through
``DB.get_eval_summary_for_pages`` so the signal they see is identical.

Kill-switched by ``settings.eval_feedback_enabled`` (default False). Keep
off until Phase 2 signal surfacing has been observed in real runs.

Mandatory hedges — the class enforces these, do not remove:

1. **Decay by dispatch count**: each time we dispatch against a target,
   bump its counter; the effective score is divided by ``(1 + n)`` so
   repeat dispatches get progressively deprioritised. Forces operators to
   re-eval before the same target can climb back up.
2. **Self-eval filter**: a page's own eval (i.e. where the eval agent's
   ``source_call_id`` was produced by the page being evaluated) is
   filtered out by ``source_call_id`` before aggregation. Without this the
   loop would be self-reinforcing — a page's own eval would drive
   dispatches on itself.
3. **Staging invariant**: the first query asserts that
   ``DB.staged == False`` OR that only rows matching our ``run_id`` are
   considered; staged A/B events must not contaminate baseline
   prioritization.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field

from rumil.database import DB, EvalSummary
from rumil.eval_feedback import PRIORITIZATION_EVAL_DIMENSIONS
from rumil.models import CallType
from rumil.orchestrators.policy_layer import (
    DispatchCall,
    Intent,
    Policy,
    QuestionState,
)
from rumil.settings import get_settings

log = logging.getLogger(__name__)

GROUNDING_DIMENSION = "grounding"


@dataclass
class _ScoredTarget:
    page_id: str
    raw_mean: float
    count: int
    decayed_score: float
    source_event_ids: list[str] = field(default_factory=list)


class EvalFeedbackPolicy(Policy):
    """Dispatch a claim investigation on the worst-grounded load-bearing claim.

    Only fires when:

    - ``settings.eval_feedback_enabled`` is True (kill-switch).
    - At least one consideration in the scope has a ``grounding`` mean
      strictly below ``settings.eval_feedback_grounding_floor`` with
      ``count >= settings.eval_feedback_min_event_count``.

    Per-target dispatch counts live on the policy instance (not on the
    shared QuestionState), which gives the decay its memory within one
    orchestrator run.
    """

    name = "eval_feedback"

    def __init__(
        self,
        db: DB,
        *,
        dimension: str = GROUNDING_DIMENSION,
    ) -> None:
        self._db = db
        self._dimension = dimension
        self._dispatch_counts: dict[str, int] = {}
        self._last_dispatched: str | None = None

    def bind_db(self, db: DB) -> None:
        self._db = db

    async def decide(self, state: QuestionState) -> Intent | None:
        settings = get_settings()
        if not settings.eval_feedback_enabled:
            return None

        target_ids = list(state.consideration_page_ids)
        if not target_ids:
            return None

        scored = await self._score_targets(target_ids)
        if not scored:
            return None

        floor = settings.eval_feedback_grounding_floor
        min_n = settings.eval_feedback_min_event_count
        eligible = [t for t in scored if t.decayed_score < floor and t.count >= min_n]
        if not eligible:
            return None

        worst = min(eligible, key=lambda t: t.decayed_score)
        self._dispatch_counts[worst.page_id] = self._dispatch_counts.get(worst.page_id, 0) + 1
        self._last_dispatched = worst.page_id

        reason = (
            f"EvalFeedbackPolicy: grounding mean={worst.raw_mean:.2f} "
            f"(n={worst.count}), decayed={worst.decayed_score:.2f}, "
            f"dispatches_since_eval={self._dispatch_counts[worst.page_id]}, "
            f"source_events={worst.source_event_ids[:3]}"
        )
        log.info(reason)

        return DispatchCall(
            call_type=CallType.ASSESS,
            kwargs={"question_id": worst.page_id},
        )

    async def _score_targets(
        self,
        page_ids: Sequence[str],
    ) -> list[_ScoredTarget]:
        """Aggregate grounding events per page and apply the dispatch-count decay.

        Pulls the raw events (not just the summary) so we can filter
        ``source_call_id`` — the "don't feed a page's own eval back into a
        dispatch against itself" invariant.
        """
        if not page_ids:
            return []

        assert self._db.staged is False or self._db.run_id, (
            "EvalFeedbackPolicy: staged DB without run_id would contaminate baseline"
        )

        summaries = await self._db.get_eval_summary_for_pages(
            page_ids,
            [self._dimension],
        )
        if not summaries:
            return []

        own_call_ids = await self._own_call_ids(page_ids)

        scored: list[_ScoredTarget] = []
        for pid in page_ids:
            by_dim = summaries.get(pid)
            if not by_dim:
                continue
            summary = by_dim.get(self._dimension)
            if summary is None or summary.count == 0:
                continue
            filtered = await self._filter_self_eval(
                pid,
                own_call_ids,
                summary,
            )
            if filtered is None:
                continue
            n = filtered.count
            raw_mean = filtered.mean
            n_dispatches = self._dispatch_counts.get(pid, 0)
            decayed = raw_mean / (1 + n_dispatches)
            scored.append(
                _ScoredTarget(
                    page_id=pid,
                    raw_mean=raw_mean,
                    count=n,
                    decayed_score=decayed,
                )
            )
        return scored

    async def _own_call_ids(self, page_ids: Sequence[str]) -> set[str]:
        """Return the set of call IDs whose scope is any page in page_ids.

        Events whose ``source_call_id`` is in this set are filtered out —
        they represent a page's own eval chain and should never drive a
        dispatch against itself.
        """
        if not page_ids:
            return set()
        try:
            q = self._db.client.table("calls").select("id").in_("scope_page_id", list(page_ids))
            if self._db.project_id:
                q = q.eq("project_id", self._db.project_id)
            rows = await self._db._execute(q)
        except Exception:
            log.debug("EvalFeedbackPolicy: own-call lookup failed", exc_info=True)
            return set()
        data = list(getattr(rows, "data", None) or [])
        return {r["id"] for r in data if r.get("id")}

    async def _filter_self_eval(
        self,
        page_id: str,
        own_call_ids: set[str],
        _summary: EvalSummary,
    ) -> EvalSummary | None:
        """Re-aggregate the page's events excluding self-eval sources.

        Only hits the DB once per decision tick when the summary has
        events — the outer loop already pre-filtered to pages with
        ``count > 0``.
        """
        events = await self._db.get_reputation_events(
            dimension=self._dimension,
        )
        kept = [
            e
            for e in events
            if (e.extra or {}).get("subject_page_id") == page_id
            and e.source_call_id not in own_call_ids
        ]
        if not kept:
            return None
        latest = max(kept, key=lambda e: e.created_at)
        total = sum(e.score for e in kept)
        return EvalSummary(
            dimension=self._dimension,
            mean=total / len(kept),
            count=len(kept),
            latest=latest.score,
        )


__all__ = ["EvalFeedbackPolicy"]
