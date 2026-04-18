"""Dump a rumil call's full trace: events + LLM exchanges verbatim.

The CC reviewer needs the model's actual words to spot confusion, so by
default this prints the full system prompt, user message, response, and
tool calls for every exchange. Use --brief to suppress large bodies.

Usage:
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.trace <call_id>
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.trace <call_id> --brief
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.trace <call_id> --only llm_exchange
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.trace <call_id> --last-n 5
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from ._format import trace_url, truncate
from ._runctx import make_db


async def _fetch_full_exchanges(db, call_id: str) -> list[dict[str, Any]]:
    """Fetch full llm_exchanges rows for a call, ordered by round."""
    rows = await db._execute(
        db.client.table("call_llm_exchanges")
        .select("*")
        .eq("call_id", call_id)
        .order("round")
        .order("created_at")
    )
    return list(getattr(rows, "data", None) or [])


def _print_header(call: dict, run_id: str) -> None:
    ct = call.get("call_type", "?")
    status = call.get("status", "?")
    cost = call.get("cost_usd")
    cost_s = f"${cost:.3f}" if cost is not None else "—"
    created = (call.get("created_at") or "")[:19].replace("T", " ")
    completed = (call.get("completed_at") or "")[:19].replace("T", " ") or "—"
    print(f"call:      {call['id']}")
    print(f"type:      {ct}")
    print(f"status:    {status}")
    print(f"cost:      {cost_s}")
    print(f"created:   {created}")
    print(f"completed: {completed}")
    print(f"scope:     {call.get('scope_page_id') or '—'}")
    print(f"parent:    {call.get('parent_call_id') or '—'}")
    print(f"trace url: {trace_url(run_id)}")
    print()


def _render_event(ev: dict, brief: bool) -> str:
    name = ev.get("event", "?")
    ts = (ev.get("ts") or "")[:19].replace("T", " ")
    head = f"  [{ts}] {name}"
    if brief:
        return head
    # Hand-picked compact renders for the high-signal events.
    if name == "context_built":
        wc = len(ev.get("working_context_page_ids") or [])
        pl = len(ev.get("preloaded_page_ids") or [])
        return f"{head}  working={wc} preloaded={pl} budget={ev.get('budget')}"
    if name == "moves_executed":
        moves = ev.get("moves") or []
        bits = [m.get("type", "?") for m in moves]
        return f"{head}  moves={bits}"
    if name == "review_complete":
        return f"{head}  fruit={ev.get('remaining_fruit')} confidence={ev.get('confidence')}"
    if name == "error":
        return f"{head}  phase={ev.get('phase')!r}  {ev.get('message', '')}"
    if name == "warning":
        return f"{head}  {ev.get('message', '')}"
    if name == "llm_exchange":
        return (
            f"{head}  phase={ev.get('phase')!r} round={ev.get('round')} "
            f"in={ev.get('input_tokens')} out={ev.get('output_tokens')} "
            f"cache_r={ev.get('cache_read_input_tokens')} "
            f"{ev.get('duration_ms') or '?'}ms "
            f"${ev.get('cost_usd') or 0:.4f}"
        )
    # Fallback: one-line JSON preview
    preview = {k: v for k, v in ev.items() if k not in {"event", "ts", "call_id"}}
    return f"{head}  {truncate(json.dumps(preview, default=str), 120)}"


def _render_exchange(ex: dict, idx: int, brief: bool) -> str:
    parts: list[str] = []
    parts.append(
        f"\n--- exchange {idx}  phase={ex.get('phase')!r}  round={ex.get('round')}  "
        f"in={ex.get('input_tokens')} out={ex.get('output_tokens')} ---"
    )
    if ex.get("error"):
        parts.append(f"ERROR: {ex['error']}")
    if brief:
        if ex.get("user_message"):
            parts.append(f"user: {truncate(ex['user_message'], 300)}")
        if ex.get("response_text"):
            parts.append(f"resp: {truncate(ex['response_text'], 300)}")
        return "\n".join(parts)
    # Full body mode — verbatim so the reviewer sees the model's actual words.
    if ex.get("system_prompt"):
        parts.append(f"\n[system_prompt]\n{ex['system_prompt']}")
    if ex.get("user_message"):
        parts.append(f"\n[user_message]\n{ex['user_message']}")
    if ex.get("response_text"):
        parts.append(f"\n[response_text]\n{ex['response_text']}")
    tool_calls = ex.get("tool_calls")
    if tool_calls:
        parts.append(f"\n[tool_calls]\n{json.dumps(tool_calls, indent=2, default=str)}")
    return "\n".join(parts)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("call_id", help="Full or short (8+ char) call ID")
    parser.add_argument("--workspace", default=None)
    parser.add_argument(
        "--brief",
        action="store_true",
        help="Shorten LLM exchanges and omit system prompt",
    )
    parser.add_argument(
        "--only",
        default=None,
        help="Filter events by name (e.g. llm_exchange, context_built, error)",
    )
    parser.add_argument(
        "--last-n",
        type=int,
        default=None,
        help="Show only the last N exchanges",
    )
    parser.add_argument(
        "--no-exchanges",
        action="store_true",
        help="Skip the verbatim exchanges section (events only)",
    )
    args = parser.parse_args()

    db, ws = await make_db(workspace=args.workspace)
    try:
        full_id = await db.resolve_call_id(args.call_id)
        if not full_id:
            print(f"no call matching {args.call_id!r}")
            sys.exit(1)
        call_rows = await db._execute(db.client.table("calls").select("*").eq("id", full_id))
        call = (getattr(call_rows, "data", None) or [None])[0]
        if not call:
            print(f"call {full_id[:8]} not found")
            sys.exit(1)

        events = await db.get_call_trace(full_id)
        exchanges = await _fetch_full_exchanges(db, full_id)
    finally:
        await db.close()

    print(f"workspace: {ws}")
    _print_header(call, call.get("run_id") or "")

    print("=== trace events ===")
    shown = [e for e in events if e.get("event") == args.only] if args.only else events
    if not shown:
        print("  (no events)")
    else:
        for ev in shown:
            print(_render_event(ev, brief=args.brief))
    print()

    if args.no_exchanges:
        return

    print("=== llm exchanges (verbatim) ===")
    if args.last_n:
        exchanges = exchanges[-args.last_n :]
    if not exchanges:
        print("(none)")
    else:
        for i, ex in enumerate(exchanges, start=1):
            print(_render_exchange(ex, i, brief=args.brief))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
