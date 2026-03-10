"""
Orchestrator: drives the research workflow using the prioritization system.
Budget is tracked here; prioritization and review calls are free.
"""

from typing import Optional

from differential.calls import run_assess, run_ingest, run_prioritization, run_scout
from differential.database import DB
from differential.models import CallType, Page, Workspace
from differential.tracer import CallTrace


DEFAULT_FRUIT_THRESHOLD = 4
DEFAULT_MAX_ROUNDS = 5
DEFAULT_INGEST_FRUIT_THRESHOLD = 5
DEFAULT_INGEST_MAX_ROUNDS = 5


def _consume_budget(db: DB) -> bool:
    """Consume one unit of global budget. Returns False if exhausted."""
    ok = db.consume_budget(1)
    if not ok:
        remaining = db.budget_remaining()
        print(f"\n[budget] Budget exhausted (remaining: {remaining}). Stopping.")
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
        print(
            f"  [orchestrator] Scout round {i + 1}/{max_rounds}, "
            f"remaining_fruit={remaining_fruit} (threshold={fruit_threshold})"
        )

        if remaining_fruit <= fruit_threshold:
            print("  [orchestrator] Fruit below threshold, stopping scout.")
            break

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
        print(
            f"  [orchestrator] Ingest round {i + 1}/{max_rounds}, "
            f"remaining_fruit={remaining_fruit} (threshold={fruit_threshold})"
        )

        if remaining_fruit <= fruit_threshold:
            print("  [orchestrator] Fruit below threshold, stopping ingest.")
            break

    return rounds


def assess_question(
    question_id: str,
    db: DB,
    parent_call_id: Optional[str] = None,
    context_page_ids: list | None = None,
) -> str | None:
    """Run one Assess call on a question. Returns call ID, or None if no budget."""
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
        indent = "  " * depth
        remaining = self.db.budget_remaining()
        actual_budget = min(budget, remaining)

        if actual_budget <= 0:
            print(
                f"{indent}[orchestrator] No budget remaining, skipping {self.db.page_label(question_id)}"
            )
            return

        print(
            f"\n{indent}=== Investigating {self.db.page_label(question_id)} | budget={actual_budget} ==="
        )

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
        p_trace: CallTrace | None = plan.get("trace")

        if not dispatches:
            # Prioritization produced no plan — fall back to simple scout+assess
            print(
                f"{indent}[orchestrator] No dispatches from prioritization, running default scout+assess"
            )
            scout_until_done(question_id, self.db, parent_call_id=p_call.id)
            assess_question(question_id, self.db, parent_call_id=p_call.id)
            return

        # Execute the plan one dispatch at a time
        budget_spent = 0
        for i, dispatch in enumerate(dispatches):
            if budget_spent >= actual_budget or self.db.budget_remaining() <= 0:
                break

            d_type = dispatch.call_type
            p = dispatch.payload
            d_question_id = p.get("question_id", question_id)
            d_budget = int(p.get("budget", 1))
            d_reason = p.get("reason", "")
            d_context_ids = p.get("context_page_ids", [])
            d_fruit_threshold = int(p.get("fruit_threshold", DEFAULT_FRUIT_THRESHOLD))
            d_max_rounds = int(p.get("max_rounds", DEFAULT_MAX_ROUNDS))

            # Resolve short IDs and validate that the question exists
            resolved = self.db.resolve_page_id(d_question_id)
            if not resolved:
                print(
                    f"{indent}  [orchestrator] Skipping dispatch — question ID not found: {d_question_id[:8]}. "
                    "Falling back to scope question."
                )
                d_question_id = question_id
            else:
                d_question_id = resolved

            d_label = self.db.page_label(d_question_id)
            if d_type == CallType.SCOUT:
                print(
                    f"{indent}  -> Dispatch: scout on {d_label} "
                    f"(fruit_threshold={d_fruit_threshold}, max_rounds={d_max_rounds}) — {d_reason}"
                )
            else:
                print(
                    f"{indent}  -> Dispatch: {d_type} on {d_label} (budget={d_budget}) — {d_reason}"
                )

            child_call_id: str | None = None

            if d_type is CallType.SCOUT:
                spent, child_ids = scout_until_done(
                    d_question_id,
                    self.db,
                    max_rounds=d_max_rounds,
                    fruit_threshold=d_fruit_threshold,
                    parent_call_id=p_call.id,
                    context_page_ids=d_context_ids,
                )
                budget_spent += spent
                child_call_id = child_ids[0] if child_ids else None

            elif d_type is CallType.ASSESS:
                child_call_id = assess_question(
                    d_question_id,
                    self.db,
                    parent_call_id=p_call.id,
                    context_page_ids=d_context_ids,
                )
                if child_call_id:
                    budget_spent += 1

            elif d_type is CallType.PRIORITIZATION:
                self.investigate_question(
                    question_id=d_question_id,
                    budget=d_budget,
                    parent_call_id=p_call.id,
                    depth=depth + 1,
                )
                budget_spent += d_budget

            if p_trace:
                trace_data: dict = {
                    "index": i,
                    "child_call_type": d_type.value,
                    "question_id": d_question_id,
                }
                if child_call_id:
                    trace_data["child_call_id"] = child_call_id
                p_trace.record("dispatch_executed", trace_data)

        if p_trace:
            p_trace.save()

    def run(self, root_question_id: str) -> None:
        """Entry point. Investigate the root question with the full budget."""
        total, used = self.db.get_budget()
        print(f"\n{'=' * 60}")
        print(f"Starting research on {self.db.page_label(root_question_id)}")
        print(f"Total budget: {total} research calls")
        print(f"{'=' * 60}\n")

        self.investigate_question(
            question_id=root_question_id,
            budget=total,
        )

        total, used = self.db.get_budget()
        print(f"\n{'=' * 60}")
        print(f"Research complete. Budget used: {used}/{total}")
        print(f"{'=' * 60}\n")
