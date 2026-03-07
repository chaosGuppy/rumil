"""Assess call: synthesise considerations and render a judgement."""
import json as _json

from differential.calls.common import (
    complete_call, dedup, format_extra_pages, print_page_ratings,
    run_closing_review, run_phase1, run_with_loading, PHASE1_TASK,
)
from differential.context import build_call_context
from differential.database import DB
from differential.executor import execute_all_moves
from differential.llm import build_system_prompt, build_user_message
from differential.models import Call, CallStatus
from differential.parser import ParsedOutput


def run_assess(
    question_id: str,
    call: Call,
    db: DB,
) -> tuple[ParsedOutput, dict]:
    """
    Run an Assess call on a question.
    Returns (parsed_output, review_dict).
    """
    print(f"\n[ASSESS] {call.id[:8]} — {db.page_label(question_id)}")

    preloaded = _json.loads(call.context_page_ids or "[]")
    system_prompt = build_system_prompt("assess")
    context_text, short_id_map = build_call_context(question_id, db, extra_page_ids=preloaded)

    task = (
        "Assess this question and render a judgement.\n\n"
        f"Question ID: `{question_id}`\n\n"
        "Synthesise the considerations, weigh evidence on multiple sides, "
        "and produce a judgement with structured confidence. "
        "Even if uncertain, commit to a position."
    )

    phase1_user = build_user_message(context_text, PHASE1_TASK)
    phase1_raw, short_load_ids = run_phase1(system_prompt, phase1_user, short_id_map, db)

    full_load_ids = [short_id_map[s] for s in short_load_ids if s in short_id_map]
    valid_load_ids = [pid for pid in full_load_ids if db.get_page(pid)]

    extra_pages_text = format_extra_pages(valid_load_ids, db)
    phase2_user = (
        (f"## Loaded Pages\n\n{extra_pages_text}\n\n---\n\n") if extra_pages_text else ""
    ) + "Perform your main task now. You may use LOAD_PAGE if you need additional pages.\n\n" + task

    messages = [
        {"role": "user",      "content": phase1_user},
        {"role": "assistant", "content": phase1_raw or "(no preliminary analysis)"},
        {"role": "user",      "content": phase2_user},
    ]
    raw, parsed, phase2_ids = run_with_loading(system_prompt, messages, short_id_map, db)

    db.update_call_status(call.id, CallStatus.RUNNING)
    created = execute_all_moves(parsed, call, db)

    all_loaded_ids = dedup(preloaded + valid_load_ids + phase2_ids)
    review = run_closing_review(call, raw, context_text, all_loaded_ids, db)
    if review:
        print(f"  [review] confidence={review.get('confidence_in_output', '?')}, "
              f"self_assessment={review.get('self_assessment', '')[:80]}")
        print_page_ratings(review, db)

    call.review_json = _json.dumps(review or {})
    complete_call(call, db, f"Assess complete. Created {len(created)} pages.")
    return parsed, review or {}
