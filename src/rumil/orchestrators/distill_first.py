"""DistillFirstOrchestrator: flip the default research loop and distill first.

The default research loop goes find_considerations -> assess -> (eventually)
distill into a view. Distill-first flips this: the first call on a sparse
question is create_view, which produces a view even from thin content and
surfaces gaps. Subsequent iterations read the view's health, pick the weakest
dimension, dispatch one targeted call to fill it, then update the view.

Loop shape, per iteration (until budget exhausted):

  1. If no view exists, call create_view_for_question.
  2. Read view health (total_pages, missing_credence, missing_importance,
     child_questions_without_judgements).
  3. Pick the weakest dimension and dispatch accordingly:
       - low total_pages           -> find_considerations
       - missing credence          -> assess on the weakest question
       - child questions unjudged  -> assess on one such child
       - else (view looks full)    -> update_view to re-distill
  4. After each mutation, update_view_for_question.

The spec references a View.health object with these metrics. The repo does
not have that yet, so this orchestrator computes health inline from DB
reads (child questions, considerations, judgements, view items). If/when
a formal ViewHealth surface lands, the _view_health() helper is the only
thing that needs to change.
"""

import logging
from dataclasses import dataclass

from rumil.database import DB
from rumil.models import CallType, Page
from rumil.orchestrators.base import BaseOrchestrator
from rumil.orchestrators.common import (
    assess_question,
    create_view_for_question,
    find_considerations_until_done,
    update_view_for_question,
)
from rumil.tracing.broadcast import Broadcaster

log = logging.getLogger(__name__)


SPARSE_PAGE_THRESHOLD = 3


@dataclass
class ViewHealth:
    view_exists: bool
    total_pages: int
    missing_credence_page_ids: list[str]
    missing_importance_item_ids: list[str]
    child_questions_without_judgements: list[str]

    @property
    def is_sparse(self) -> bool:
        return self.total_pages < SPARSE_PAGE_THRESHOLD

    @property
    def has_gaps(self) -> bool:
        return bool(
            self.missing_credence_page_ids
            or self.missing_importance_item_ids
            or self.child_questions_without_judgements
            or self.is_sparse
        )


class DistillFirstOrchestrator(BaseOrchestrator):
    """Distill-first orchestrator: view is the planning surface, not the output.

    On each iteration, reads view health, dispatches one call that targets
    the weakest dimension, then refreshes the view.
    """

    def __init__(
        self,
        db: DB,
        broadcaster: Broadcaster | None = None,
        budget_cap: int | None = None,
    ):
        super().__init__(db, broadcaster)
        self._budget_cap = budget_cap
        self._parent_call_id: str | None = None

    async def _budget_remaining(self) -> int:
        remaining = await self.db.budget_remaining()
        if self._budget_cap is not None:
            return min(remaining, self._budget_cap)
        return remaining

    async def run(self, root_question_id: str) -> None:
        await self._setup()
        try:
            while await self._budget_remaining() > 0:
                did_work = await self._iterate(root_question_id)
                if not did_work:
                    break
        finally:
            await self._teardown()

    async def _iterate(self, question_id: str) -> bool:
        """Run one loop iteration. Returns False if no useful work was done."""
        view = await self.db.get_view_for_question(question_id)
        if view is None:
            call_id = await create_view_for_question(
                question_id,
                self.db,
                parent_call_id=self._parent_call_id,
                broadcaster=self.broadcaster,
            )
            return call_id is not None

        health = await self._view_health(question_id, view)
        dispatched = await self._dispatch_for_gap(question_id, health)
        if not dispatched:
            return False

        await update_view_for_question(
            question_id,
            self.db,
            parent_call_id=self._parent_call_id,
            broadcaster=self.broadcaster,
            force=True,
        )
        return True

    async def _dispatch_for_gap(self, question_id: str, health: ViewHealth) -> bool:
        """Pick the weakest dimension and dispatch one call. Returns True if dispatched."""
        if health.is_sparse:
            log.info(
                "DistillFirst: question=%s is sparse (total_pages=%d), "
                "dispatching find_considerations",
                question_id[:8],
                health.total_pages,
            )
            rounds, _ = await find_considerations_until_done(
                question_id,
                self.db,
                parent_call_id=self._parent_call_id,
                broadcaster=self.broadcaster,
            )
            return rounds > 0

        if health.missing_credence_page_ids:
            target = health.missing_credence_page_ids[0]
            log.info(
                "DistillFirst: question=%s has page missing credence (%s), dispatching assess",
                question_id[:8],
                target[:8],
            )
            call_id = await assess_question(
                target,
                self.db,
                parent_call_id=self._parent_call_id,
                broadcaster=self.broadcaster,
            )
            return call_id is not None

        if health.child_questions_without_judgements:
            target = health.child_questions_without_judgements[0]
            log.info(
                "DistillFirst: question=%s has child (%s) without judgement, dispatching assess",
                question_id[:8],
                target[:8],
            )
            call_id = await assess_question(
                target,
                self.db,
                parent_call_id=self._parent_call_id,
                broadcaster=self.broadcaster,
            )
            return call_id is not None

        log.info(
            "DistillFirst: question=%s view looks full, re-distilling via update_view",
            question_id[:8],
        )
        call_id = await update_view_for_question(
            question_id,
            self.db,
            parent_call_id=self._parent_call_id,
            broadcaster=self.broadcaster,
        )
        return call_id is not None

    async def _view_health(self, question_id: str, view: Page) -> ViewHealth:
        """Compute gap metrics for the view from the current DB state.

        Reads are batched where possible. The helper is the only place that
        mints ViewHealth, so it's the single swap point if a formal
        view-health surface lands later.
        """
        considerations = await self.db.get_considerations_for_question(question_id)
        consideration_pages = [p for p, _ in considerations]
        child_questions = await self.db.get_child_questions(question_id)

        missing_credence = [p.id for p in consideration_pages if p.credence is None]

        view_items_with_links = await self.db.get_view_items(view.id)
        missing_importance = [p.id for p, link in view_items_with_links if link.importance is None]

        child_ids = [c.id for c in child_questions]
        judgements_by_q = await self.db.get_judgements_for_questions(child_ids) if child_ids else {}
        unjudged_children = [cid for cid in child_ids if not judgements_by_q.get(cid)]

        total_pages = len(consideration_pages) + len(child_questions)

        return ViewHealth(
            view_exists=True,
            total_pages=total_pages,
            missing_credence_page_ids=missing_credence,
            missing_importance_item_ids=missing_importance,
            child_questions_without_judgements=unjudged_children,
        )
