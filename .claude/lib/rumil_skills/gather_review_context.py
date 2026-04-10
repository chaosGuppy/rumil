"""Gather all the context Claude needs to produce a structured review of a question.

This is the `!`-block helper for `/rumil-review`. It pulls:

1. The question's full research subtree (sub-questions, claims, judgements)
2. Every call that has targeted the question (recent-first)
3. For each call, a compact trace summary — event list + the final
   exchange's response_text (truncated)
4. Any existing confusion-scan verdicts from the scan log

The output is designed as one big context dump for Claude to read and
then produce a punch list. Not LLM-powered itself.

Usage:
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.gather_review_context <qid>
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.gather_review_context <qid> --call-limit 12
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

from rumil.summary import build_research_tree

from ._format import truncate
from ._runctx import make_db
from .scan_log import get_scan, load_scan_log


async def _recent_calls(db, question_id: str, limit: int) -> list[dict[str, Any]]:
    rows = await db._execute(
        db.client.table("calls")
        .select(
            "id,call_type,status,cost_usd,created_at,completed_at,"
            "trace_json,result_summary,review_json"
        )
        .eq("scope_page_id", question_id)
        .order("created_at", desc=True)
        .limit(limit)
    )
    return list(getattr(rows, "data", None) or [])


async def _final_exchange(db, call_id: str) -> dict[str, Any] | None:
    rows = await db._execute(
        db.client.table("call_llm_exchanges")
        .select("phase,round,response_text,error")
        .eq("call_id", call_id)
        .order("round", desc=True)
        .order("created_at", desc=True)
        .limit(1)
    )
    data = getattr(rows, "data", None) or []
    return data[0] if data else None


def _format_call_brief(
    call: dict[str, Any],
    final_ex: dict[str, Any] | None,
    scan: dict[str, Any] | None,
) -> str:
    parts: list[str] = []
    short = call["id"][:8]
    ct = call.get("call_type", "?")
    status = call.get("status", "?")
    cost = call.get("cost_usd")
    cost_s = f"${cost:.3f}" if cost is not None else "—"
    created = (call.get("created_at") or "")[:19].replace("T", " ")
    parts.append(f"### call {short}  {ct}  {status}  {cost_s}  {created}")

    if scan:
        v = scan.get("verdict", "?")
        sev = scan.get("severity")
        sev_s = f" s{sev}" if sev is not None else ""
        sym = scan.get("primary_symptom") or "—"
        parts.append(f"  scanner verdict: [{v}{sev_s}] {sym}")
        for ev in scan.get("evidence", [])[:2]:
            parts.append(f"    · {ev}")
        action = scan.get("suggested_action")
        if action:
            parts.append(f"    → {action}")

    trace = call.get("trace_json") or []
    if trace:
        events = [e.get("event", "?") for e in trace]
        parts.append(f"  events: {events}")
        errors = [e for e in trace if e.get("event") == "error"]
        for e in errors[:2]:
            parts.append(f"    ERROR: {truncate(e.get('message', ''), 120)}")

    if final_ex:
        if final_ex.get("error"):
            parts.append(f"  final exchange error: {truncate(final_ex['error'], 200)}")
        resp = final_ex.get("response_text") or ""
        if resp:
            parts.append(
                f"  final response (phase={final_ex.get('phase')!r}, "
                f"round={final_ex.get('round')}):"
            )
            parts.append(truncate(resp, 600))

    result_summary = call.get("result_summary") or ""
    if result_summary:
        parts.append(f"  result_summary: {truncate(result_summary, 300)}")

    review_json = call.get("review_json") or {}
    if isinstance(review_json, dict) and review_json:
        # Extract the interesting review fields without dumping the full JSON.
        for key in ("confidence_in_output", "remaining_fruit", "what_was_missing"):
            if key in review_json:
                val = review_json[key]
                if isinstance(val, str):
                    val = truncate(val, 200)
                parts.append(f"  review.{key}: {val}")

    return "\n".join(parts)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("question_id", help="Full or short question ID")
    parser.add_argument("--workspace", default=None)
    parser.add_argument("--depth", type=int, default=4)
    parser.add_argument(
        "--call-limit",
        type=int,
        default=12,
        help="How many recent calls to summarize",
    )
    args = parser.parse_args()

    db, ws = await make_db(workspace=args.workspace)
    try:
        full_id = await db.resolve_page_id(args.question_id)
        if not full_id:
            print(f"no question matching {args.question_id!r} in workspace {ws!r}")
            sys.exit(1)
        question = await db.get_page(full_id)
        if question is None:
            print(f"page {full_id[:8]} vanished mid-lookup")
            sys.exit(1)

        print(f"workspace: {ws}")
        print(f"question:  {full_id[:8]}  {truncate(question.headline, 80)}")
        print()

        print("=== research subtree ===")
        tree = await build_research_tree(full_id, db, max_depth=args.depth)
        print(tree.rstrip())
        print()

        print("=== recent calls on this question ===")
        calls = await _recent_calls(db, full_id, args.call_limit)
        if not calls:
            print("(none)")
        else:
            scan_log_data = load_scan_log()
            for call in calls:
                final_ex = await _final_exchange(db, call["id"])
                scan = get_scan(scan_log_data, call["id"])
                print()
                print(_format_call_brief(call, final_ex, scan))
        print()

        print("=== review instructions ===")
        print(
            "Read the subtree and call briefs above. Produce a structured "
            "punch list of problems worth fixing. For each item:"
        )
        print("  - which call / which page (short ID)")
        print("  - severity (1-5)")
        print("  - what's wrong in one sentence")
        print("  - suggested action: dispatch, apply_move, edit_prompt, ignore")
    finally:
        await db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
