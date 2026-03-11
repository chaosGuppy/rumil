"""Prioritization call: plan budget allocation across questions."""

import logging

from differential.calls.common import complete_call, run_call
from differential.context import build_prioritization_context, collect_subtree_ids
from differential.database import DB
from differential.models import Call, CallType
from differential.trace_events import ContextBuiltEvent, DispatchesPlannedEvent
from differential.tracer import CallTrace

log = logging.getLogger(__name__)


async def run_prioritization(
    scope_question_id: str,
    call: Call,
    budget: int,
    db: DB,
    broadcaster=None,
) -> dict:
    """Run a Prioritization call.

    Returns a summary dict including the list of dispatches and trace.
    """
    trace = CallTrace(call.id, db, broadcaster=broadcaster)
    log.info(
        "Prioritization starting: call=%s, question=%s, budget=%d",
        call.id[:8], scope_question_id[:8], budget,
    )
    context_text, short_id_map = await build_prioritization_context(
        db, scope_question_id=scope_question_id
    )
    subtree_ids = await collect_subtree_ids(scope_question_id, db)
    await trace.record(ContextBuiltEvent(budget=budget))

    task = (
        f"You have a budget of **{budget} research calls** to allocate on this question.\n\n"
        f"Scope question ID: `{scope_question_id}`\n\n"
        "Review the current state of the workspace above and decide how to spend the budget. "
        "You may also create subquestions (using create_question + link_child_question) to "
        "decompose the scope question before dispatching research on them. "
        "Dispatch is restricted to questions within this scope subtree. "
        "Use the dispatch tool to allocate calls."
    )

    result = await run_call(
        CallType.PRIORITIZATION,
        task,
        context_text,
        call,
        db,
        subtree_ids=subtree_ids,
        short_id_map=short_id_map,
        trace=trace,
    )

    await trace.record(DispatchesPlannedEvent(
        dispatches=[
            {
                "call_type": d.call_type.value,
                **d.payload.model_dump(exclude_defaults=True),
            }
            for d in result.dispatches
        ],
    ))

    summary = {
        "dispatches": result.dispatches,
        "moves_created": len(result.moves),
        "trace": trace,
    }

    log.info(
        "Prioritization complete: call=%s, dispatches=%d, moves=%d",
        call.id[:8], len(result.dispatches), len(result.moves),
    )
    await complete_call(
        call,
        db,
        f"Prioritization complete. Planned {len(result.dispatches)} dispatches.",
        trace=trace,
    )
    return summary
