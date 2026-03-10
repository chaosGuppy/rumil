"""Scout call: find missing considerations on a question."""

from differential.calls.common import (
    RunCallResult,
    complete_call,
    extract_loaded_page_ids,
    format_moves_for_review,
    moves_to_trace_data,
    print_page_ratings,
    run_call,
    run_closing_review,
)
from differential.context import build_call_context
from differential.database import DB
from differential.models import Call, CallStatus, CallType
from differential.tracer import CallTrace


def run_scout(
    question_id: str,
    call: Call,
    db: DB,
) -> tuple[RunCallResult, dict]:
    """Run a Scout call on a question.

    Returns (run_call_result, review_dict).
    """
    trace = CallTrace(call.id, db)
    print(f"\n[SCOUT] {call.id[:8]} — {db.page_label(question_id)}")

    preloaded = call.context_page_ids or []
    context_text, _, working_page_ids = build_call_context(
        question_id, db, extra_page_ids=preloaded
    )
    trace.record(
        "context_built",
        {
            "working_context_page_ids": working_page_ids,
            "preloaded_page_ids": preloaded,
        },
    )

    task = (
        "Scout for missing considerations on this question.\n\n"
        f"Question ID (use this when linking considerations): `{question_id}`"
    )

    db.update_call_status(call.id, CallStatus.RUNNING)
    result = run_call(CallType.SCOUT, task, context_text, call, db)
    if result.phase1_page_ids:
        trace.record("phase1_loaded", {"page_ids": result.phase1_page_ids})
    phase2_loaded = extract_loaded_page_ids(result, db)
    if phase2_loaded:
        trace.record("phase2_loaded", {"page_ids": phase2_loaded})
    trace.record(
        "moves_executed", moves_to_trace_data(result.moves, result.created_page_ids)
    )

    all_loaded_ids = list(
        dict.fromkeys(preloaded + result.phase1_page_ids + phase2_loaded)
    )
    review_context = format_moves_for_review(result.moves)
    review = run_closing_review(call, review_context, context_text, all_loaded_ids, db)
    remaining_fruit = 5
    if review:
        remaining_fruit = review.get("remaining_fruit", 5)
        print(
            f"  [review] remaining_fruit={remaining_fruit}, "
            f"confidence={review.get('confidence_in_output', '?')}"
        )
        print_page_ratings(review, db)
        trace.record(
            "review_complete",
            {
                "remaining_fruit": remaining_fruit,
                "confidence": review.get("confidence_in_output"),
            },
        )

    call.review_json = review or {}
    complete_call(
        call,
        db,
        f"Scout complete. Created {len(result.created_page_ids)} pages. Remaining fruit: {remaining_fruit}",
        trace=trace,
    )
    return result, review or {}
