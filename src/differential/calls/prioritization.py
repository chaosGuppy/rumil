"""Prioritization call: plan budget allocation across questions."""
from differential.calls.common import complete_call
from differential.context import build_prioritization_context
from differential.database import DB
from differential.executor import execute_all_moves
from differential.llm import run_call
from differential.models import Call
from differential.parser import parse_output


def run_prioritization(
    scope_question_id: str,
    call: Call,
    budget: int,
    db: DB,
) -> dict:
    """
    Run a Prioritization call.
    Returns a summary dict including the list of dispatches.
    """
    print(f"\n[PRIORITIZATION] {call.id[:8]} — {db.page_label(scope_question_id)} — budget {budget}")

    context_text = build_prioritization_context(db, scope_question_id=scope_question_id)

    task = (
        f"You have a budget of **{budget} research calls** to allocate on this question.\n\n"
        f"Scope question ID: `{scope_question_id}`\n\n"
        "Review the current state of the workspace above and decide how to spend the budget. "
        "Output your plan as a sequence of <dispatch> tags."
    )

    raw = run_call(call_type="prioritization", task_description=task, context_text=context_text)

    parsed = parse_output(raw)
    execute_all_moves(parsed, call, db)

    summary = {
        "dispatches": parsed.dispatches,
        "moves_created": len(parsed.moves),
    }

    complete_call(call, db, f"Prioritization complete. Planned {len(parsed.dispatches)} dispatches.")
    return summary
