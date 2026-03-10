"""Ingest call: extract considerations from a source document."""

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
from differential.models import Call, CallStatus, CallType, Page
from differential.tracer import CallTrace

log = logging.getLogger(__name__)


def run_ingest(
    source_page: Page,
    question_id: str,
    call: Call,
    db: DB,
) -> tuple[RunCallResult, dict]:
    """Run an Ingest call: extract considerations from a source document.

    Returns (run_call_result, review_dict).
    """
    trace = CallTrace(call.id, db)
    extra = source_page.extra or {}
    filename = extra.get("filename", source_page.id[:8])
    log.info(
        "Ingest starting: call=%s, source=%s (%s), question=%s",
        call.id[:8], source_page.id[:8], filename, question_id[:8],
    )

    preloaded = call.context_page_ids or []
    question_context, _, working_page_ids = build_call_context(
        question_id, db, extra_page_ids=preloaded
    )
    trace.record(
        "context_built",
        {
            "working_context_page_ids": working_page_ids,
            "preloaded_page_ids": preloaded,
            "source_page_id": source_page.id,
        },
    )

    source_section = (
        "\n\n---\n\n## Source Document\n\n"
        f"**File:** {filename}  \n"
        f"**Source page ID:** `{source_page.id}`\n\n"
        f"{source_page.content}"
    )
    context_text = question_context + source_section

    task = (
        "Extract considerations from the source document above for this question.\n\n"
        f"Question ID: `{question_id}`\n"
        f"Source page ID: `{source_page.id}`"
    )

    db.update_call_status(call.id, CallStatus.RUNNING)
    result = run_call(CallType.INGEST, task, context_text, call, db)
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
    if review:
        log.info(
            "Ingest review: confidence=%s, remaining_fruit=%s",
            review.get("confidence_in_output", "?"),
            review.get("remaining_fruit", "?"),
        )
        log_page_ratings(review, db)
        trace.record(
            "review_complete",
            {
                "remaining_fruit": review.get("remaining_fruit"),
                "confidence": review.get("confidence_in_output"),
            },
        )

    call.review_json = review or {}
    log.info(
        "Ingest complete: call=%s, pages_created=%d, source=%s",
        call.id[:8], len(result.created_page_ids), filename,
    )
    complete_call(
        call,
        db,
        f"Ingest complete. Created {len(result.created_page_ids)} pages from '{filename}'.",
        trace=trace,
    )
    return result, review or {}
