"""Prioritization call: plan budget allocation across questions."""

from differential.calls.common import complete_call, moves_to_trace_data, run_call
from differential.context import build_prioritization_context
from differential.database import DB
from differential.models import Call, CallType
from differential.tracer import CallTrace


def run_prioritization(
    scope_question_id: str,
    call: Call,
    budget: int,
    db: DB,
) -> dict:
    """Run a Prioritization call.

    Returns a summary dict including the list of dispatches and trace.
    """
    trace = CallTrace(call.id, db)
    print(
        f"\n[PRIORITIZATION] {call.id[:8]} — {db.page_label(scope_question_id)} — budget {budget}"
    )

    context_text = build_prioritization_context(db, scope_question_id=scope_question_id)
    trace.record("context_built", {"budget": budget})

    task = (
        f"You have a budget of **{budget} research calls** to allocate on this question.\n\n"
        f"Scope question ID: `{scope_question_id}`\n\n"
        "Review the current state of the workspace above and decide how to spend the budget. "
        "Use the dispatch tool to allocate calls."
    )

    result = run_call(CallType.PRIORITIZATION, task, context_text, call, db)

    trace.record("dispatches_planned", {
        "dispatches": [
            {
                "call_type": d.call_type.value,
                "question_id": d.payload.get("question_id", ""),
                "budget": d.payload.get("budget", 1),
                "reason": d.payload.get("reason", ""),
            }
            for d in result.dispatches
        ],
    })
    trace.record("moves_executed", moves_to_trace_data(result.moves, result.created_page_ids))

    summary = {
        "dispatches": result.dispatches,
        "moves_created": len(result.moves),
        "trace": trace,
    }

    complete_call(
        call,
        db,
        f"Prioritization complete. Planned {len(result.dispatches)} dispatches.",
        trace=trace,
    )
    return summary
