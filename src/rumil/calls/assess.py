"""Assess call: synthesise considerations and render a judgement."""

import logging

from rumil.calls.common import (
    RunCallResult,
    complete_call,
    extract_loaded_page_ids,
    format_moves_for_review,
    log_page_ratings,
    resolve_page_refs,
    run_call,
    run_closing_review,
)
from rumil.context import (
    build_context_for_question,
    format_preloaded_pages,
)
from rumil.database import DB
from rumil.models import Call, CallStatus, CallType
from rumil.tracing.trace_events import ContextBuiltEvent, ReviewCompleteEvent
from rumil.tracing.tracer import CallTrace

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
    working_context, working_page_ids = await build_context_for_question(
        question_id, db,
    )
    if preloaded:
        working_context += await format_preloaded_pages(preloaded, db)

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
    result = await run_call(
        CallType.ASSESS, task, working_context, call, db, trace=trace,
    )
    phase2_loaded = await extract_loaded_page_ids(result, db)

    all_loaded_summaries = list(result.loaded_page_summaries)
    for pid in phase2_loaded:
        page = await db.get_page(pid)
        if page:
            all_loaded_summaries.append((pid, page.summary))
    for pid in preloaded:
        page = await db.get_page(pid)
        if page and not any(s[0] == pid for s in all_loaded_summaries):
            all_loaded_summaries.append((pid, page.summary))

    review_context = format_moves_for_review(result.moves)
    review = await run_closing_review(
        call, review_context, working_context, all_loaded_summaries, db, trace,
        scope_question_id=question_id,
    )
    if not review.error:
        log.info(
            "Assess review: confidence=%s, self_assessment=%s",
            review.data.get("confidence_in_output", "?"),
            review.data.get("self_assessment", "")[:80],
        )
        await log_page_ratings(review.data, db)
        await trace.record(ReviewCompleteEvent(
            remaining_fruit=review.data.get("remaining_fruit"),
            confidence=review.data.get("confidence_in_output"),
        ))

    call.review_json = review.data
    log.info(
        "Assess complete: call=%s, pages_created=%d",
        call.id[:8], len(result.created_page_ids),
    )
    await complete_call(
        call,
        db,
        f"Assess complete. Created {len(result.created_page_ids)} pages.",
    )
    return result, review.data
