"""
Orchestrator: drives the research workflow using the prioritization system.
Budget is tracked here; prioritization and review calls are free.
"""

import logging
from typing import Optional

from differential.calls import run_assess, run_ingest, run_prioritization, run_scout
from differential.database import DB
from differential.models import (
    AssessDispatchPayload,
    CallType,
    Page,
    PrioritizationDispatchPayload,
    ScoutDispatchPayload,
    Workspace,
)
from differential.tracer import CallTrace


log = logging.getLogger(__name__)

DEFAULT_FRUIT_THRESHOLD = 4
DEFAULT_MAX_ROUNDS = 5
DEFAULT_INGEST_FRUIT_THRESHOLD = 5
DEFAULT_INGEST_MAX_ROUNDS = 5


def _consume_budget(db: DB) -> bool:
    """Consume one unit of global budget. Returns False if exhausted."""
    ok = db.consume_budget(1)
    if not ok:
        remaining = db.budget_remaining()
        log.info("Budget exhausted (remaining: %d)", remaining)
    return ok


def scout_until_done(
    question_id: str,
    db: DB,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
    fruit_threshold: int = DEFAULT_FRUIT_THRESHOLD,
    parent_call_id: Optional[str] = None,
    context_page_ids: list | None = None,
) -> tuple[int, list[str]]:
    """
    Run Scout rounds until remaining_fruit falls below fruit_threshold or max_rounds
    is reached. Returns (rounds_made, list_of_call_ids).
    fruit_threshold is the primary stopping condition; max_rounds is a failsafe.
    """
    log.info(
        "scout_until_done: question=%s, max_rounds=%d, fruit_threshold=%d",
        question_id[:8], max_rounds, fruit_threshold,
    )
    rounds = 0
    call_ids: list[str] = []
    for i in range(max_rounds):
        if not _consume_budget(db):
            break

        call = db.create_call(
            CallType.SCOUT,
            scope_page_id=question_id,
            parent_call_id=parent_call_id,
            context_page_ids=context_page_ids,
        )
        call_ids.append(call.id)
        _, review = run_scout(question_id, call, db)
        rounds += 1

        remaining_fruit = review.get("remaining_fruit", 5) if review else 5
        log.info(
            "Scout round %d/%d: remaining_fruit=%d (threshold=%d)",
            i + 1, max_rounds, remaining_fruit, fruit_threshold,
        )

        if remaining_fruit <= fruit_threshold:
            log.info(
                "Scout fruit (%d) below threshold (%d), stopping",
                remaining_fruit, fruit_threshold,
            )
            break

    log.info("scout_until_done finished: %d rounds, %d calls", rounds, len(call_ids))
    return rounds, call_ids


def ingest_until_done(
    source_page: Page,
    question_id: str,
    db: DB,
    max_rounds: int = DEFAULT_INGEST_MAX_ROUNDS,
    fruit_threshold: int = DEFAULT_INGEST_FRUIT_THRESHOLD,
    parent_call_id: Optional[str] = None,
) -> int:
    """
    Run Ingest rounds on a source/question pair until remaining_fruit falls below
    fruit_threshold or max_rounds is reached. Returns number of Ingest calls made.
    fruit_threshold is the primary stopping condition; max_rounds is a failsafe.
    Each round sees previously extracted claims via the question's working context.
    """
    log.info(
        "ingest_until_done: source=%s, question=%s, max_rounds=%d",
        source_page.id[:8], question_id[:8], max_rounds,
    )
    rounds = 0
    for i in range(max_rounds):
        if not _consume_budget(db):
            break

        call = db.create_call(
            CallType.INGEST,
            scope_page_id=source_page.id,
            parent_call_id=parent_call_id,
        )
        _, review = run_ingest(source_page, question_id, call, db)
        rounds += 1

        remaining_fruit = review.get("remaining_fruit", 5) if review else 5
        log.info(
            "Ingest round %d/%d: remaining_fruit=%d (threshold=%d)",
            i + 1, max_rounds, remaining_fruit, fruit_threshold,
        )

        if remaining_fruit <= fruit_threshold:
            log.info(
                "Ingest fruit (%d) below threshold (%d), stopping",
                remaining_fruit, fruit_threshold,
            )
            break

    log.info("ingest_until_done finished: %d rounds", rounds)
    return rounds


def assess_question(
    question_id: str,
    db: DB,
    parent_call_id: Optional[str] = None,
    context_page_ids: list | None = None,
) -> str | None:
    """Run one Assess call on a question. Returns call ID, or None if no budget."""
    log.info("assess_question: question=%s", question_id[:8])
    if not _consume_budget(db):
        return None

    call = db.create_call(
        CallType.ASSESS,
        scope_page_id=question_id,
        parent_call_id=parent_call_id,
        context_page_ids=context_page_ids,
    )
    run_assess(question_id, call, db)
    return call.id


class Orchestrator:
    def __init__(self, db: DB):
        self.db = db

    def investigate_question(
        self,
        question_id: str,
        budget: int,
        parent_call_id: Optional[str] = None,
        depth: int = 0,
    ) -> None:
        """
        Core recursive investigation loop.
        - Runs a Prioritization call to plan the budget allocation
        - Executes the plan (Scout, Assess, sub-Prioritization)
        """
        remaining = self.db.budget_remaining()
        actual_budget = min(budget, remaining)
        log.info(
            "investigate_question: question=%s, budget=%d, actual=%d, depth=%d",
            question_id[:8], budget, actual_budget, depth,
        )

        if actual_budget <= 0:
            log.info(
                "No budget remaining, skipping question=%s", question_id[:8],
            )
            return

        # Run a prioritization call (free) to get a plan
        p_call = self.db.create_call(
            CallType.PRIORITIZATION,
            scope_page_id=question_id,
            parent_call_id=parent_call_id,
            budget_allocated=actual_budget,
            workspace=Workspace.PRIORITIZATION,
        )

        plan = run_prioritization(
            scope_question_id=question_id,
            call=p_call,
            budget=actual_budget,
            db=self.db,
        )

        dispatches = plan.get("dispatches", [])
        log.debug(
            "Prioritization produced %d dispatches for question=%s",
            len(dispatches), question_id[:8],
        )
        p_trace: CallTrace | None = plan.get("trace")

        if not dispatches:
            log.info(
                "No dispatches from prioritization, running default scout+assess "
                "for question=%s", question_id[:8],
            )
            scout_until_done(question_id, self.db, parent_call_id=p_call.id)
            assess_question(question_id, self.db, parent_call_id=p_call.id)
            return

        # Execute the plan one dispatch at a time
        budget_spent = 0
        for i, dispatch in enumerate(dispatches):
            if budget_spent >= actual_budget or self.db.budget_remaining() <= 0:
                break

            p = dispatch.payload

            resolved = self.db.resolve_page_id(p.question_id)
            if not resolved:
                log.warning(
                    "Dispatch question ID not found: %s, falling back to scope",
                    p.question_id[:8],
                )
                resolved = question_id

            d_label = self.db.page_label(resolved)
            child_call_id: str | None = None

            if isinstance(p, ScoutDispatchPayload):
                log.info(
                    "Dispatch: scout on %s (fruit_threshold=%d, max_rounds=%d) — %s",
                    d_label, p.fruit_threshold, p.max_rounds, p.reason,
                )
                spent, child_ids = scout_until_done(
                    resolved,
                    self.db,
                    max_rounds=p.max_rounds,
                    fruit_threshold=p.fruit_threshold,
                    parent_call_id=p_call.id,
                    context_page_ids=p.context_page_ids,
                )
                budget_spent += spent
                child_call_id = child_ids[0] if child_ids else None

            elif isinstance(p, AssessDispatchPayload):
                log.info("Dispatch: assess on %s — %s", d_label, p.reason)
                child_call_id = assess_question(
                    resolved,
                    self.db,
                    parent_call_id=p_call.id,
                    context_page_ids=p.context_page_ids,
                )
                if child_call_id:
                    budget_spent += 1

            elif isinstance(p, PrioritizationDispatchPayload):
                log.info(
                    "Dispatch: prioritization on %s (budget=%d) — %s",
                    d_label, p.budget, p.reason,
                )
                self.investigate_question(
                    question_id=resolved,
                    budget=p.budget,
                    parent_call_id=p_call.id,
                    depth=depth + 1,
                )
                budget_spent += p.budget

            if p_trace:
                trace_data: dict = {
                    "index": i,
                    "child_call_type": dispatch.call_type.value,
                    "question_id": resolved,
                }
                if child_call_id:
                    trace_data["child_call_id"] = child_call_id
                p_trace.record("dispatch_executed", trace_data)

        if p_trace:
            p_trace.save()

    def run(self, root_question_id: str) -> None:
        """Entry point. Investigate the root question with the full budget."""
        total, used = self.db.get_budget()
        log.info(
            "Orchestrator.run starting: root_question=%s, budget=%d",
            root_question_id[:8], total,
        )

        self.investigate_question(
            question_id=root_question_id,
            budget=total,
        )

        total, used = self.db.get_budget()
        log.info("Orchestrator.run complete: budget used %d/%d", used, total)
