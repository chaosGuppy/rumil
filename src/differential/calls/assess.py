"""Assess call: synthesise considerations and render a judgement."""

import logging

from differential.calls.common import (
    RunCallResult,
    complete_call,
    extract_loaded_page_ids,
    format_moves_for_review,
    log_page_ratings,
    resolve_page_refs,
    run_call,
    run_closing_review,
)
from differential.context import build_call_context
from differential.database import DB
from differential.models import Call, CallStatus, CallType
from differential.tracing.trace_events import ContextBuiltEvent, ReviewCompleteEvent
from differential.tracing.tracer import CallTrace

log = logging.getLogger(__name__)


async def run_assess(
    question_id: str,
    call: Call,
    db: DB,
    broadcaster=None,
) -> tuple[RunCallResult, dict]:
    """Run an Assess call on a question.

    Returns (run_call_result, review_dict).
    """
    trace = CallTrace(call.id, db, broadcaster=broadcaster)
    log.info("Assess starting: call=%s, question=%s", call.id[:8], question_id[:8])

    preloaded = call.context_page_ids or []
    context_text, _, working_page_ids = await build_call_context(
        question_id, db, extra_page_ids=preloaded
    )
    await trace.record(ContextBuiltEvent(
        working_context_page_ids=await resolve_page_refs(working_page_ids, db),
        preloaded_page_ids=await resolve_page_refs(preloaded, db),
    ))

    task = (
        "Assess this question and render a judgement.\n\n"
        f"Question ID: `{question_id}`\n\n"
        "Synthesise the considerations, weigh evidence on multiple sides, "
        "and produce a judgement with structured confidence. "
        "Even if uncertain, commit to a position."
    )

    await db.update_call_status(call.id, CallStatus.RUNNING)
    result = await run_call(CallType.ASSESS, task, context_text, call, db, trace=trace)
    phase2_loaded = await extract_loaded_page_ids(result, db)

    all_loaded_ids = list(
        dict.fromkeys(preloaded + result.phase1_page_ids + phase2_loaded)
    )
    review_context = format_moves_for_review(result.moves)
    review = await run_closing_review(call, review_context, context_text, all_loaded_ids, db, trace)
    if review:
        log.info(
            "Assess review: confidence=%s, self_assessment=%s",
            review.get("confidence_in_output", "?"),
            review.get("self_assessment", "")[:80],
        )
        await log_page_ratings(review, db)
        await trace.record(ReviewCompleteEvent(
            remaining_fruit=review.get("remaining_fruit"),
            confidence=review.get("confidence_in_output"),
        ))

    call.review_json = review or {}
    log.info(
        "Assess complete: call=%s, pages_created=%d",
        call.id[:8], len(result.created_page_ids),
    )
    await complete_call(
        call,
        db,
        f"Assess complete. Created {len(result.created_page_ids)} pages.",
    )
    return result, review or {}
