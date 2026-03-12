"""Scout call: find missing considerations on a question."""

import logging

from differential.calls.common import (
    RunCallResult,
    auto_unlink_unhelpful_pages,
    complete_call,
    extract_loaded_page_ids,
    format_moves_for_review,
    log_page_ratings,
    resolve_page_refs,
    run_call,
    run_closing_review,
)
from differential.context import (
    build_context_for_question,
    format_preloaded_pages,
)
from differential.database import DB
from differential.models import Call, CallStatus, CallType, ScoutMode
from differential.tracing.trace_events import ContextBuiltEvent, ReviewCompleteEvent
from differential.tracing.tracer import CallTrace

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
    broadcaster=None,
    max_rounds: int | None = None,
    fruit_threshold: int | None = None,
) -> tuple[RunCallResult, dict]:
    """Run a Scout call on a question.

    Returns (run_call_result, review_dict).
    """
    call.call_params = {"mode": mode.value}
    if max_rounds is not None:
        call.call_params["max_rounds"] = max_rounds
    if fruit_threshold is not None:
        call.call_params["fruit_threshold"] = fruit_threshold
    trace = CallTrace(call.id, db, broadcaster=broadcaster)
    log.info(
        "Scout starting: call=%s, question=%s, mode=%s",
        call.id[:8], question_id[:8], mode.value,
    )

    working_context, working_page_ids = await build_context_for_question(
        question_id, db,
    )

    preloaded = call.context_page_ids or []
    if preloaded:
        working_context += await format_preloaded_pages(preloaded, db)

    await trace.record(ContextBuiltEvent(
        working_context_page_ids=await resolve_page_refs(working_page_ids, db),
        preloaded_page_ids=await resolve_page_refs(preloaded, db),
        scout_mode=mode.value,
    ))

    mode_instruction = _CONCRETE_INSTRUCTION if mode == ScoutMode.CONCRETE else ''
    task = (
        f"Scout for missing considerations on this question.{mode_instruction}\n\n"
        f"Question ID (use this when linking considerations): `{question_id}`"
    )

    await db.update_call_status(call.id, CallStatus.RUNNING)
    result = await run_call(
        CallType.SCOUT, task, working_context, call, db, trace=trace,
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
    remaining_fruit = 5
    if not review.error:
        remaining_fruit = review.data.get("remaining_fruit", 5)
        log.info(
            "Scout review: remaining_fruit=%d, confidence=%s",
            remaining_fruit, review.data.get("confidence_in_output", "?"),
        )
        await log_page_ratings(review.data, db)
        await trace.record(ReviewCompleteEvent(
            remaining_fruit=remaining_fruit,
            confidence=review.data.get("confidence_in_output"),
        ))
        await auto_unlink_unhelpful_pages(review.data, call.scope_page_id, db)

    call.review_json = review.data
    log.info(
        "Scout complete: call=%s, pages_created=%d, fruit=%d",
        call.id[:8], len(result.created_page_ids), remaining_fruit,
    )
    await complete_call(
        call,
        db,
        f"Scout complete. Created {len(result.created_page_ids)} pages. Remaining fruit: {remaining_fruit}",
    )
    return result, review.data
