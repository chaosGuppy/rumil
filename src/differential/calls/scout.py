"""Scout call: find missing considerations on a question."""

import logging

from differential.calls.common import (
    RunCallResult,
    complete_call,
    extract_loaded_page_ids,
    format_moves_for_review,
    log_page_ratings,
    moves_to_trace_data,
    run_call,
    run_closing_review,
)
from differential.context import build_call_context
from differential.database import DB
from differential.models import Call, CallStatus, CallType, ScoutMode
from differential.tracer import CallTrace

log = logging.getLogger(__name__)


_CONCRETE_INSTRUCTION = (
    '\n\n**Mode: CONCRETE**\n\n'
    'Your goal is considerations, sub-questions, and hypotheses that are as specific '
    'and falsifiable as possible. Concreteness means: named actors, specific timeframes, '
    'quantitative claims, named mechanisms, particular cases. A concrete claim should be '
    'possible to be clearly wrong about — that is what makes it valuable.\n\n'
    'Concrete scouts are expected to produce claims that subsequent investigation may '
    'refute. That is a feature, not a failure. Do not hedge your way back to vagueness.'
)


async def run_scout(
    question_id: str,
    call: Call,
    db: DB,
    mode: ScoutMode = ScoutMode.ABSTRACT,
) -> tuple[RunCallResult, dict]:
    """Run a Scout call on a question.

    Returns (run_call_result, review_dict).
    """
    trace = CallTrace(call.id, db)
    log.info(
        "Scout starting: call=%s, question=%s, mode=%s",
        call.id[:8], question_id[:8], mode.value,
    )

    preloaded = call.context_page_ids or []
    context_text, _, working_page_ids = await build_call_context(
        question_id, db, extra_page_ids=preloaded
    )
    trace.record(
        "context_built",
        {
            "working_context_page_ids": working_page_ids,
            "preloaded_page_ids": preloaded,
            "scout_mode": mode.value,
        },
    )

    mode_instruction = _CONCRETE_INSTRUCTION if mode == ScoutMode.CONCRETE else ''
    task = (
        f"Scout for missing considerations on this question.{mode_instruction}\n\n"
        f"Question ID (use this when linking considerations): `{question_id}`"
    )

    await db.update_call_status(call.id, CallStatus.RUNNING)
    result = await run_call(CallType.SCOUT, task, context_text, call, db)
    if result.phase1_page_ids:
        trace.record("phase1_loaded", {"page_ids": result.phase1_page_ids})
    phase2_loaded = await extract_loaded_page_ids(result, db)
    if phase2_loaded:
        trace.record("phase2_loaded", {"page_ids": phase2_loaded})
    trace.record(
        "moves_executed", moves_to_trace_data(result.moves, result.created_page_ids)
    )

    all_loaded_ids = list(
        dict.fromkeys(preloaded + result.phase1_page_ids + phase2_loaded)
    )
    review_context = format_moves_for_review(result.moves)
    review = await run_closing_review(call, review_context, context_text, all_loaded_ids, db)
    remaining_fruit = 5
    if review:
        remaining_fruit = review.get("remaining_fruit", 5)
        log.info(
            "Scout review: remaining_fruit=%d, confidence=%s",
            remaining_fruit, review.get("confidence_in_output", "?"),
        )
        await log_page_ratings(review, db)
        trace.record(
            "review_complete",
            {
                "remaining_fruit": remaining_fruit,
                "confidence": review.get("confidence_in_output"),
            },
        )

    call.review_json = review or {}
    log.info(
        "Scout complete: call=%s, pages_created=%d, fruit=%d",
        call.id[:8], len(result.created_page_ids), remaining_fruit,
    )
    await complete_call(
        call,
        db,
        f"Scout complete. Created {len(result.created_page_ids)} pages. Remaining fruit: {remaining_fruit}",
        trace=trace,
    )
    return result, review or {}
