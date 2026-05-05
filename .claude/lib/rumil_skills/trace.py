"""Dump a rumil call's full trace: events + LLM exchanges verbatim.

The CC reviewer needs the model's actual words to spot confusion, so by
default this prints the full system prompt, user message, response, and
tool calls for every exchange. Use --brief to suppress large bodies.

Usage:
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.trace <call_id>
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.trace <call_id> --brief
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.trace <call_id> --only llm_exchange
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.trace <call_id> --last-n 5
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.trace <call_id> --system-once
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.trace <call_id> --user-only 2000
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.trace <call_id> --response-only 2000
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


def _tail(text: str, n: int) -> str:
    """Return the last n chars of `text`, prefixed with a truncation marker if trimmed."""
    total = len(text)
    if total <= n:
        return text
    return f"... (truncated, {total} chars total)\n{text[-n:]}"


def _render_exchange(
    ex: dict,
    idx: int,
    brief: bool,
    *,
    system_once_state: dict | None = None,
    user_only: int | None = None,
    response_only: int | None = None,
) -> str:
    """Render one exchange.

    `system_once_state` (if provided) is a mutable dict shared across calls
    that tracks the first system prompt seen and which exchange index it
    appeared in; subsequent identical prompts get a placeholder.
    """
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
    sys_prompt = ex.get("system_prompt")
    if sys_prompt:
        if system_once_state is not None:
            first = system_once_state.get("first_prompt")
            first_idx = system_once_state.get("first_idx")
            if first is None:
                system_once_state["first_prompt"] = sys_prompt
                system_once_state["first_idx"] = idx
                parts.append(f"\n[system_prompt]\n{sys_prompt}")
            elif sys_prompt == first:
                parts.append(
                    f"\n[system_prompt]\n(system prompt unchanged from exchange {first_idx})"
                )
            else:
                parts.append(f"\n[system_prompt]\n{sys_prompt}")
        else:
            parts.append(f"\n[system_prompt]\n{sys_prompt}")
    user_msg = ex.get("user_message")
    if user_msg:
        body = _tail(user_msg, user_only) if user_only is not None else user_msg
        parts.append(f"\n[user_message]\n{body}")
    response = ex.get("response_text")
    if response:
        body = _tail(response, response_only) if response_only is not None else response
        parts.append(f"\n[response_text]\n{body}")
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
    parser.add_argument(
        "--system-once",
        action="store_true",
        help=(
            "Render the system prompt only the first time it appears; "
            "replace subsequent identical occurrences with a placeholder."
        ),
    )
    parser.add_argument(
        "--user-only",
        type=int,
        default=None,
        metavar="N",
        help="Show only the last N chars of each user_message (with truncation marker)",
    )
    parser.add_argument(
        "--response-only",
        type=int,
        default=None,
        metavar="N",
        help="Show only the last N chars of each response_text (with truncation marker)",
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
        system_once_state: dict | None = {} if args.system_once else None
        for i, ex in enumerate(exchanges, start=1):
            print(
                _render_exchange(
                    ex,
                    i,
                    brief=args.brief,
                    system_once_state=system_once_state,
                    user_only=args.user_only,
                    response_only=args.response_only,
                )
            )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
