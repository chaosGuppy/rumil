"""
Call types: Scout, Assess, Prioritization, Ingest.
Each call loads context, runs the LLM, parses output, executes moves,
runs a closing review, and returns a result summary.

Scout, Assess, and Ingest use a two-phase pattern:
  Phase 1 (free): instance sees workspace map + working context, may request
                  additional pages via LOAD_PAGE, writes planning notes.
  Phase 2 (costs 1 budget unit): continuing conversation with any requested
                  pages appended; instance does the real work.
"""
import json as _json
from datetime import datetime
from typing import Optional

from context import build_call_context, build_context_for_question, build_prioritization_context, format_page
from database import DB
from executor import execute_all_moves
from llm import build_system_prompt, build_user_message, run_call, run_llm
from models import Call, CallStatus, Page, Workspace
from parser import ParsedOutput, parse_output

REVIEW_SYSTEM_PROMPT = """\
You are a research assistant completing a closing review of a call you just made \
in a collaborative research workspace. Be honest and specific in your self-assessment."""

# Phase 1 task prompt — instructs the instance to request pages and plan
_PHASE1_TASK = (
    "Perform your preliminary analysis now. Review the workspace map above and use "
    "LOAD_PAGE moves if you need full content from other pages. Write any planning "
    "notes. The main task description will follow in the next turn."
)


def _dedup(ids: list[str]) -> list[str]:
    """Deduplicate a list of IDs preserving order."""
    seen: set[str] = set()
    result = []
    for x in ids:
        if x not in seen:
            seen.add(x)
            result.append(x)
    return result


def _print_page_ratings(review: dict) -> None:
    ratings = review.get("page_ratings", [])
    if not ratings:
        return
    score_labels = {-1: "confusing", 0: "no help", 1: "helpful", 2: "very helpful"}
    for r in ratings:
        pid = r.get("page_id", "?")
        score = r.get("score", "?")
        note = r.get("note", "")
        label = score_labels.get(score, str(score))
        print(f"  [page] {pid} [{label}]: {note}")


def _complete_call(call: Call, db: DB, summary: str) -> None:
    call.status = CallStatus.COMPLETE
    call.completed_at = datetime.utcnow()
    call.result_summary = summary
    db.save_call(call)


def _run_closing_review(
    call: Call,
    main_output: str,
    context_text: str,
    loaded_page_ids: Optional[list[str]] = None,
    db: Optional[DB] = None,
) -> Optional[dict]:
    """
    Run the closing review as a separate prompt. Free (not counted against budget).
    The instance reflects on its own work with the original context and output in view.
    If loaded_page_ids are provided, the review also asks for per-page usefulness ratings.
    Returns parsed review dict or None.
    """
    # Build page rating section if any pages were loaded
    page_rating_prompt = ""
    if loaded_page_ids and db:
        page_lines = []
        for pid in loaded_page_ids:
            page = db.get_page(pid)
            if page:
                page_lines.append(f'  - `{pid[:8]}`: "{page.summary[:120]}"')
        if page_lines:
            page_rating_prompt = (
                f'\n  "page_ratings": [\n'
                f'    {{"page_id": "SHORT_ID", "score": <-1|0|1|2>, '
                f'"note": "<one sentence>"}}\n'
                f'  ],\n'
            )
            page_rating_prompt = (
                f"\n\nThe following pages were loaded into your context beyond the base "
                f"working context:\n" + "\n".join(page_lines) +
                f"\n\nFor each, include a rating in your review JSON:\n"
                f'  "page_ratings": [\n'
                f'    {{"page_id": "SHORT_ID", "score": N, "note": "one sentence"}}\n'
                f'  ]\n'
                f"Scores: -1 = actively confusing, 0 = didn't help, "
                f"1 = helped, 2 = extremely helpful"
            )

    page_ratings_field = (
        '  "page_ratings": [{"page_id": "...", "score": N, "note": "..."}],  // if pages were loaded\n'
        if loaded_page_ids else ""
    )

    review_task = (
        f"You have just completed a {call.call_type.value} call.\n\n"
        f"Here is your output from that call:\n{main_output}\n\n"
        f"Please produce a closing review in this exact format:\n\n"
        f"<review>\n"
        f"{{\n"
        f'  "remaining_fruit": <0-10 integer — how much useful work remains on this scope>\n'
        f'    // Scale: 0 = nothing more to add; 1-2 = close to exhausted, only marginal additions expected;\n'
        f'    // 3-4 = most significant angles covered, incremental gains likely;\n'
        f'    // 5-6 = good coverage so far, diminishing but real returns expected;\n'
        f'    // 7-8 = substantial work remains, clear gaps visible; 9-10 = barely started\n'
        f'  "confidence_in_output": <0-5 float>,\n'
        f'  "context_was_adequate": <true/false>,\n'
        f'  "what_was_missing": "<optional: what additional context would have helped>",\n'
        f'  "tensions_noticed": "<optional: any conflicts or inconsistencies you noticed>",\n'
        f'  "self_assessment": "<1-2 sentences on how this call went>",\n'
        f'  "suggested_next_steps": "<optional: what should happen next>"\n'
        f"{page_ratings_field}"
        f"}}\n"
        f"</review>"
        f"{page_rating_prompt}"
    )

    user_message = build_user_message(context_text, review_task)

    try:
        review_raw = run_llm(
            system_prompt=REVIEW_SYSTEM_PROMPT,
            user_message=user_message,
            max_tokens=1024,
        )
        review = parse_output(review_raw).review
        if review and db:
            for r in review.get("page_ratings", []):
                pid = db.resolve_page_id(r.get("page_id", ""))
                score = r.get("score")
                if pid and isinstance(score, int):
                    db.save_page_rating(pid, call.id, score, r.get("note", ""))
        return review
    except Exception as e:
        status = getattr(e, "status_code", None)
        reason = f"HTTP {status}" if status else type(e).__name__
        print(f"  [review] Closing review skipped after retries ({reason}) — continuing.")
        return None


def _run_phase1(
    system_prompt: str,
    phase1_user_msg: str,
) -> tuple[str, list[str]]:
    """Preliminary analysis. Returns (raw_response, short_load_ids). Free."""
    try:
        raw = run_llm(
            system_prompt=system_prompt,
            user_message=phase1_user_msg,
            max_tokens=2048,
        )
        load_ids = parse_output(raw).load_page_ids
        if load_ids:
            print(f"  [phase1] Requested pages: {load_ids}")
        return raw, load_ids
    except Exception as e:
        status = getattr(e, "status_code", None)
        reason = f"HTTP {status}" if status else type(e).__name__
        print(f"  [phase1] Phase 1 skipped after retries ({reason}) — continuing.")
        return "", []


def _format_extra_pages(page_ids: list[str], db: DB) -> str:
    """Format a list of pages (full UUIDs) as readable text for phase 2 context."""
    parts = []
    for pid in page_ids:
        page = db.get_page(pid)
        if page:
            parts.append(f"### Page `{pid[:8]}`\n\n{format_page(page, db=db)}")
    return "\n\n---\n\n".join(parts)


_MAX_LOAD_ROUNDS = 3  # cap on iterative LOAD_PAGE rounds within a single phase 2


def _resolve_load_requests(
    parsed: ParsedOutput,
    short_id_map: dict[str, str],
    db: DB,
) -> list[str]:
    """Return valid full UUIDs for all LOAD_PAGE moves in a parsed response.

    Handles both short IDs (8 chars, from workspace map) and full UUIDs.
    Checks parsed.moves directly — more robust than relying on parsed.load_page_ids,
    which can be empty if the payload JSON failed to parse cleanly.
    """
    valid_ids = []
    seen: set[str] = set()
    for move in parsed.moves:
        if move.move_type != "LOAD_PAGE":
            continue
        # Try payload first; fall back to raw string if JSON gave a parse error
        pid = move.payload.get("page_id", "")
        if not pid:
            # Payload may be {"_parse_error": ..., "_raw": "abc12345"} — extract raw
            pid = move.payload.get("_raw", "").strip().strip('"')
        pid = pid.strip()
        if not pid or pid in seen:
            continue
        seen.add(pid)
        # Resolve: short ID via map first, then try as full UUID directly
        full_id = short_id_map.get(pid) or (pid if db.get_page(pid) else None)
        if full_id:
            valid_ids.append(full_id)
    return valid_ids


def _run_with_loading(
    system_prompt: str,
    initial_messages: list[dict],
    short_id_map: dict[str, str],
    db: DB,
    max_tokens: int = 4096,
) -> tuple[str, ParsedOutput, list[str]]:
    """Phase 2: run the LLM with iterative LOAD_PAGE support.

    If the model requests pages in its response, they are fetched and appended
    as a new user turn, up to _MAX_LOAD_ROUNDS times. All API calls within this
    loop count as a single budget unit.
    Returns (final_raw, final_parsed, phase2_loaded_ids).
    """
    messages = list(initial_messages)
    all_loaded: list[str] = []

    for round_num in range(_MAX_LOAD_ROUNDS + 1):
        raw = run_llm(system_prompt=system_prompt, messages=messages, max_tokens=max_tokens)
        parsed = parse_output(raw)

        load_page_moves = [m for m in parsed.moves if m.move_type == "LOAD_PAGE"]
        if not load_page_moves:
            return raw, parsed, all_loaded  # No loading requested — done

        if round_num == _MAX_LOAD_ROUNDS:
            print(f"  [phase2] Max load rounds ({_MAX_LOAD_ROUNDS}) reached — proceeding.")
            return raw, parsed, all_loaded

        valid_ids = _resolve_load_requests(parsed, short_id_map, db)
        all_loaded.extend(valid_ids)
        print(f"  [phase2] Round {round_num + 1}: loading {len(valid_ids)} page(s) "
              f"({len(load_page_moves)} requested)...")

        extra_text = _format_extra_pages(valid_ids, db) if valid_ids else ""
        is_last = (round_num + 1 == _MAX_LOAD_ROUNDS)
        follow_up = (
            (f"## Additional Loaded Pages\n\n{extra_text}\n\n---\n\n" if extra_text else "")
            + ("This is the final loading round — complete your task now, do not use LOAD_PAGE further."
               if is_last else "Continue with your task.")
        )

        messages.append({"role": "assistant", "content": raw})
        messages.append({"role": "user",      "content": follow_up})

    return raw, parsed, all_loaded  # unreachable but satisfies type checker


def run_scout(
    question_id: str,
    call: Call,
    db: DB,
) -> tuple[ParsedOutput, dict]:
    """
    Run a Scout call on a question.
    Returns (parsed_output, review_dict).
    """
    print(f"\n[SCOUT] {call.id[:8]} — question {question_id[:8]}")

    preloaded = _json.loads(call.context_page_ids or "[]")
    system_prompt = build_system_prompt("scout")
    context_text, short_id_map = build_call_context(question_id, db, extra_page_ids=preloaded)

    task = (
        f"Scout for missing considerations on this question.\n\n"
        f"Question ID (use this when linking considerations): `{question_id}`"
    )

    # Phase 1: preliminary analysis + optional LOAD_PAGE requests
    phase1_user = build_user_message(context_text, _PHASE1_TASK)
    phase1_raw, short_load_ids = _run_phase1(system_prompt, phase1_user)

    # Resolve short IDs → full UUIDs; validate they exist in the DB
    full_load_ids = [short_id_map[s] for s in short_load_ids if s in short_id_map]
    valid_load_ids = [pid for pid in full_load_ids if db.get_page(pid)]

    extra_pages_text = _format_extra_pages(valid_load_ids, db)
    phase2_user = (
        (f"## Loaded Pages\n\n{extra_pages_text}\n\n---\n\n") if extra_pages_text else ""
    ) + "Perform your main task now. You may use LOAD_PAGE if you need additional pages.\n\n" + task

    # Phase 2: real work, with iterative LOAD_PAGE support
    messages = [
        {"role": "user",      "content": phase1_user},
        {"role": "assistant", "content": phase1_raw or "(no preliminary analysis)"},
        {"role": "user",      "content": phase2_user},
    ]
    raw, parsed, phase2_ids = _run_with_loading(system_prompt, messages, short_id_map, db)

    db.update_call_status(call.id, CallStatus.RUNNING)
    created = execute_all_moves(parsed, call, db)

    all_loaded_ids = _dedup(preloaded + valid_load_ids + phase2_ids)
    review = _run_closing_review(call, raw, context_text, all_loaded_ids, db)
    remaining_fruit = 5
    if review:
        remaining_fruit = review.get("remaining_fruit", 5)
        print(f"  [review] remaining_fruit={remaining_fruit}, "
              f"confidence={review.get('confidence_in_output', '?')}")
        _print_page_ratings(review)

    call.review_json = _json.dumps(review or {})
    _complete_call(call, db, f"Scout complete. Created {len(created)} pages. Remaining fruit: {remaining_fruit}")
    return parsed, review or {}


def run_assess(
    question_id: str,
    call: Call,
    db: DB,
) -> tuple[ParsedOutput, dict]:
    """
    Run an Assess call on a question.
    Returns (parsed_output, review_dict).
    """
    print(f"\n[ASSESS] {call.id[:8]} — question {question_id[:8]}")

    preloaded = _json.loads(call.context_page_ids or "[]")
    system_prompt = build_system_prompt("assess")
    context_text, short_id_map = build_call_context(question_id, db, extra_page_ids=preloaded)

    task = (
        f"Assess this question and render a judgement.\n\n"
        f"Question ID: `{question_id}`\n\n"
        f"Synthesise the considerations, weigh evidence on multiple sides, "
        f"and produce a judgement with structured confidence. "
        f"Even if uncertain, commit to a position."
    )

    # Phase 1: preliminary analysis + optional LOAD_PAGE requests
    phase1_user = build_user_message(context_text, _PHASE1_TASK)
    phase1_raw, short_load_ids = _run_phase1(system_prompt, phase1_user)

    full_load_ids = [short_id_map[s] for s in short_load_ids if s in short_id_map]
    valid_load_ids = [pid for pid in full_load_ids if db.get_page(pid)]

    extra_pages_text = _format_extra_pages(valid_load_ids, db)
    phase2_user = (
        (f"## Loaded Pages\n\n{extra_pages_text}\n\n---\n\n") if extra_pages_text else ""
    ) + "Perform your main task now. You may use LOAD_PAGE if you need additional pages.\n\n" + task

    # Phase 2: real work, with iterative LOAD_PAGE support
    messages = [
        {"role": "user",      "content": phase1_user},
        {"role": "assistant", "content": phase1_raw or "(no preliminary analysis)"},
        {"role": "user",      "content": phase2_user},
    ]
    raw, parsed, phase2_ids = _run_with_loading(system_prompt, messages, short_id_map, db)

    db.update_call_status(call.id, CallStatus.RUNNING)
    created = execute_all_moves(parsed, call, db)

    all_loaded_ids = _dedup(preloaded + valid_load_ids + phase2_ids)
    review = _run_closing_review(call, raw, context_text, all_loaded_ids, db)
    if review:
        print(f"  [review] confidence={review.get('confidence_in_output', '?')}, "
              f"self_assessment={review.get('self_assessment', '')[:80]}")
        _print_page_ratings(review)

    call.review_json = _json.dumps(review or {})
    _complete_call(call, db, f"Assess complete. Created {len(created)} pages.")
    return parsed, review or {}


def run_prioritization(
    scope_question_id: str,
    call: Call,
    budget: int,
    db: DB,
) -> dict:
    """
    Run a Prioritization call.
    The instance reads the workspace state and decides how to allocate its budget.
    Returns a summary dict including the list of dispatches.
    """
    print(f"\n[PRIORITIZATION] {call.id[:8]} — scope {scope_question_id[:8]} — budget {budget}")

    context_text = build_prioritization_context(db, scope_question_id=scope_question_id)

    task = (
        f"You have a budget of **{budget} research calls** to allocate on this question.\n\n"
        f"Scope question ID: `{scope_question_id}`\n\n"
        f"Review the current state of the workspace above and decide how to spend the budget. "
        f"Output your plan as a sequence of <dispatch> tags."
    )

    raw = run_call(call_type="prioritization", task_description=task, context_text=context_text)

    parsed = parse_output(raw)
    execute_all_moves(parsed, call, db)

    summary = {
        "dispatches": parsed.dispatches,
        "moves_created": len(parsed.moves),
    }

    _complete_call(call, db, f"Prioritization complete. Planned {len(parsed.dispatches)} dispatches.")
    return summary


def run_ingest(
    source_page: Page,
    question_id: str,
    call: Call,
    db: DB,
) -> tuple[ParsedOutput, dict]:
    """
    Run an Ingest call: extract considerations from a source document for a question.
    Returns (parsed_output, review_dict).
    """
    extra = _json.loads(source_page.extra) if source_page.extra else {}
    filename = extra.get("filename", source_page.id[:8])

    print(f"\n[INGEST] {call.id[:8]} — source '{filename}' -> question {question_id[:8]}")

    preloaded = _json.loads(call.context_page_ids or "[]")
    system_prompt = build_system_prompt("ingest")

    # Workspace map + question working context
    question_context, short_id_map = build_call_context(question_id, db, extra_page_ids=preloaded)

    # Source document appended as a fixed section (always needed for ingest)
    source_section = (
        f"\n\n---\n\n## Source Document\n\n"
        f"**File:** {filename}  \n"
        f"**Source page ID:** `{source_page.id}`\n\n"
        f"{source_page.content}"
    )
    context_text = question_context + source_section

    task = (
        f"Extract considerations from the source document above for this question.\n\n"
        f"Question ID: `{question_id}`\n"
        f"Source page ID: `{source_page.id}`"
    )

    # Phase 1: preliminary analysis + optional LOAD_PAGE requests
    phase1_user = build_user_message(context_text, _PHASE1_TASK)
    phase1_raw, short_load_ids = _run_phase1(system_prompt, phase1_user)

    full_load_ids = [short_id_map[s] for s in short_load_ids if s in short_id_map]
    valid_load_ids = [pid for pid in full_load_ids if db.get_page(pid)]

    extra_pages_text = _format_extra_pages(valid_load_ids, db)
    phase2_user = (
        (f"## Loaded Pages\n\n{extra_pages_text}\n\n---\n\n") if extra_pages_text else ""
    ) + "Perform your main task now. You may use LOAD_PAGE if you need additional pages.\n\n" + task

    # Phase 2: real work, with iterative LOAD_PAGE support
    messages = [
        {"role": "user",      "content": phase1_user},
        {"role": "assistant", "content": phase1_raw or "(no preliminary analysis)"},
        {"role": "user",      "content": phase2_user},
    ]
    raw, parsed, phase2_ids = _run_with_loading(system_prompt, messages, short_id_map, db)

    db.update_call_status(call.id, CallStatus.RUNNING)
    created = execute_all_moves(parsed, call, db)

    all_loaded_ids = _dedup(preloaded + valid_load_ids + phase2_ids)
    review = _run_closing_review(call, raw, context_text, all_loaded_ids, db)
    if review:
        print(f"  [review] confidence={review.get('confidence_in_output', '?')}, "
              f"remaining_fruit={review.get('remaining_fruit', '?')}")
        _print_page_ratings(review)

    call.review_json = _json.dumps(review or {})
    _complete_call(call, db, f"Ingest complete. Created {len(created)} pages from '{filename}'.")
    return parsed, review or {}
