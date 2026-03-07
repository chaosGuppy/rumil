"""Shared utilities for call types."""

from datetime import datetime

from anthropic.types import MessageParam

from differential.context import format_page
from differential.database import DB
from differential.llm import build_user_message, run_llm
from differential.models import Call, CallStatus
from differential.parser import ParsedOutput, parse_output

REVIEW_SYSTEM_PROMPT = (
    "You are a research assistant completing a closing review of a call you just made "
    "in a collaborative research workspace. Be honest and specific in your self-assessment."
)

PHASE1_TASK = (
    "Perform your preliminary analysis now. Review the workspace map above and use "
    "LOAD_PAGE moves if you need full content from other pages. Write any planning "
    "notes. The main task description will follow in the next turn."
)

MAX_LOAD_ROUNDS = 3


def dedup(ids: list[str]) -> list[str]:
    """Deduplicate a list of IDs preserving order."""
    seen: set[str] = set()
    result = []
    for x in ids:
        if x not in seen:
            seen.add(x)
            result.append(x)
    return result


def print_page_ratings(review: dict, db: DB) -> None:
    ratings = review.get("page_ratings", [])
    if not ratings:
        return
    score_labels = {-1: "confusing", 0: "no help", 1: "helpful", 2: "very helpful"}
    for r in ratings:
        pid = r.get("page_id", "?")
        resolved = db.resolve_page_id(pid) if pid != "?" else None
        page_label = db.page_label(resolved or pid) if resolved else f"[{pid}]"
        score = r.get("score", "?")
        note = r.get("note", "")
        label = score_labels.get(score, str(score))
        print(f"  [page] {page_label} [{label}]: {note}")


def complete_call(call: Call, db: DB, summary: str) -> None:
    call.status = CallStatus.COMPLETE
    call.completed_at = datetime.utcnow()
    call.result_summary = summary
    db.save_call(call)


def run_closing_review(
    call: Call,
    main_output: str,
    context_text: str,
    loaded_page_ids: list[str] | None = None,
    db: DB | None = None,
) -> dict | None:
    """
    Run the closing review as a separate prompt. Free (not counted against budget).
    """
    page_rating_prompt = ""
    if loaded_page_ids and db:
        page_lines = []
        for pid in loaded_page_ids:
            page = db.get_page(pid)
            if page:
                page_lines.append(f'  - `{pid[:8]}`: "{page.summary[:120]}"')
        if page_lines:
            page_rating_prompt = (
                "\n\nThe following pages were loaded into your context beyond the base "
                "working context:\n" + "\n".join(page_lines) +
                "\n\nFor each, include a rating in your review JSON:\n"
                '  "page_ratings": [\n'
                '    {"page_id": "SHORT_ID", "score": N, "note": "one sentence"}\n'
                '  ]\n'
                "Scores: -1 = actively confusing, 0 = didn't help, "
                "1 = helped, 2 = extremely helpful"
            )

    page_ratings_field = (
        '  "page_ratings": [{"page_id": "...", "score": N, "note": "..."}],  // if pages were loaded\n'
        if loaded_page_ids else ""
    )

    review_task = (
        f"You have just completed a {call.call_type.value} call.\n\n"
        f"Here is your output from that call:\n{main_output}\n\n"
        "Please produce a closing review in this exact format:\n\n"
        "<review>\n"
        "{\n"
        '  "remaining_fruit": <0-10 integer — how much useful work remains on this scope>\n'
        '    // Scale: 0 = nothing more to add; 1-2 = close to exhausted, only marginal additions expected;\n'
        '    // 3-4 = most significant angles covered, incremental gains likely;\n'
        '    // 5-6 = good coverage so far, diminishing but real returns expected;\n'
        '    // 7-8 = substantial work remains, clear gaps visible; 9-10 = barely started\n'
        '  "confidence_in_output": <0-5 float>,\n'
        '  "context_was_adequate": <true/false>,\n'
        '  "what_was_missing": "<optional: what additional context would have helped>",\n'
        '  "tensions_noticed": "<optional: any conflicts or inconsistencies you noticed>",\n'
        '  "self_assessment": "<1-2 sentences on how this call went>",\n'
        '  "suggested_next_steps": "<optional: what should happen next>"\n'
        f"{page_ratings_field}"
        "}\n"
        "</review>"
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


def run_phase1(
    system_prompt: str,
    phase1_user_msg: str,
    short_id_map: dict[str, str] | None = None,
    db: DB | None = None,
) -> tuple[str, list[str]]:
    """Preliminary analysis. Returns (raw_response, short_load_ids). Free."""
    try:
        raw = run_llm(
            system_prompt=system_prompt,
            user_message=phase1_user_msg,
            max_tokens=2048,
        )
        load_ids = parse_output(raw).load_page_ids
        if load_ids and db and short_id_map:
            labels = [db.page_label(short_id_map[s]) if s in short_id_map else f'[{s}]'
                      for s in load_ids]
            print(f"  [phase1] Requested pages: {', '.join(labels)}")
        elif load_ids:
            print(f"  [phase1] Requested pages: {load_ids}")
        return raw, load_ids
    except Exception as e:
        status = getattr(e, "status_code", None)
        reason = f"HTTP {status}" if status else type(e).__name__
        print(f"  [phase1] Phase 1 skipped after retries ({reason}) — continuing.")
        return "", []


def format_extra_pages(page_ids: list[str], db: DB) -> str:
    """Format a list of pages (full UUIDs) as readable text for phase 2 context."""
    parts = []
    for pid in page_ids:
        page = db.get_page(pid)
        if page:
            parts.append(f"### Page `{pid[:8]}`\n\n{format_page(page, db=db)}")
    return "\n\n---\n\n".join(parts)


def resolve_load_requests(
    parsed: ParsedOutput,
    short_id_map: dict[str, str],
    db: DB,
) -> list[str]:
    """Return valid full UUIDs for all LOAD_PAGE moves in a parsed response."""
    valid_ids = []
    seen: set[str] = set()
    for move in parsed.moves:
        if move.move_type != "LOAD_PAGE":
            continue
        pid = move.payload.get("page_id", "")
        if not pid:
            pid = move.payload.get("_raw", "").strip().strip('"')
        pid = pid.strip()
        if not pid or pid in seen:
            continue
        seen.add(pid)
        full_id = short_id_map.get(pid) or (pid if db.get_page(pid) else None)
        if full_id:
            valid_ids.append(full_id)
    return valid_ids


def run_with_loading(
    system_prompt: str,
    initial_messages: list[MessageParam],
    short_id_map: dict[str, str],
    db: DB,
    max_tokens: int = 4096,
) -> tuple[str, ParsedOutput, list[str]]:
    """Phase 2: run the LLM with iterative LOAD_PAGE support."""
    messages = list(initial_messages)
    all_loaded: list[str] = []

    for round_num in range(MAX_LOAD_ROUNDS + 1):
        raw = run_llm(system_prompt=system_prompt, messages=messages, max_tokens=max_tokens)
        parsed = parse_output(raw)

        load_page_moves = [m for m in parsed.moves if m.move_type == "LOAD_PAGE"]
        if not load_page_moves:
            return raw, parsed, all_loaded

        if round_num == MAX_LOAD_ROUNDS:
            print(f"  [phase2] Max load rounds ({MAX_LOAD_ROUNDS}) reached — proceeding.")
            return raw, parsed, all_loaded

        valid_ids = resolve_load_requests(parsed, short_id_map, db)
        all_loaded.extend(valid_ids)
        print(f"  [phase2] Round {round_num + 1}: loading {len(valid_ids)} page(s) "
              f"({len(load_page_moves)} requested)...")

        extra_text = format_extra_pages(valid_ids, db) if valid_ids else ""
        is_last = (round_num + 1 == MAX_LOAD_ROUNDS)
        follow_up = (
            (f"## Additional Loaded Pages\n\n{extra_text}\n\n---\n\n" if extra_text else "")
            + ("This is the final loading round — complete your task now, do not use LOAD_PAGE further."
               if is_last else "Continue with your task.")
        )

        messages.append({"role": "assistant", "content": raw})
        messages.append({"role": "user",      "content": follow_up})

    raise RuntimeError("Unreachable: loading loop exhausted without returning")
