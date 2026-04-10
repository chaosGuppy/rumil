"""Manage the cc-mediated chat envelope Call.

The envelope is a single CLAUDE_CODE_DIRECT Call that owns every mutation
made during a CC chat session. It exists so that moves applied from the
broader CC context have a well-defined home in the trace and are clearly
distinguishable from rumil-internal work.

Usage:
    # Show current envelope status
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.chat_envelope status

    # Start / ensure an envelope (optionally scoped to a question)
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.chat_envelope start [<qid>]

    # Clear the envelope (next start creates a fresh one)
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.chat_envelope clear
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from ._format import print_trace
from ._runctx import (
    clear_chat_envelope,
    ensure_chat_envelope,
    load_session_state,
    make_db,
)


async def cmd_status() -> None:
    state = load_session_state()
    env = state.chat_envelope
    print(f"workspace: {state.workspace}")
    if not env:
        print("envelope:  (none)")
        return
    print(f"envelope:  call={env['call_id'][:8]} run={env['run_id'][:8]}")
    print(f"workspace: {env['workspace']}")
    print(f"started:   {env.get('started_at', '—')}")
    print_trace(env["run_id"], label="trace url")

    db, _ = await make_db(workspace=env["workspace"])
    try:
        call = await db.get_call(env["call_id"])
        if call is None:
            print("status:    STALE (envelope call row missing)")
            return
        print(f"status:    {call.status.value}")
        print(f"scope:     {call.scope_page_id or '—'}")
    finally:
        await db.close()


async def cmd_start(scope_question_id: str | None) -> None:
    resolved_qid = None
    if scope_question_id:
        db, _ = await make_db()
        try:
            resolved_qid = await db.resolve_page_id(scope_question_id)
        finally:
            await db.close()
        if not resolved_qid:
            print(f"no question matching {scope_question_id!r}")
            sys.exit(1)

    db, call = await ensure_chat_envelope(scope_question_id=resolved_qid)
    try:
        print(f"envelope:  call={call.id[:8]} run={db.run_id[:8]}")
        print(f"scope:     {call.scope_page_id or '—'}")
        print_trace(db.run_id, label="trace url")
    finally:
        await db.close()


def cmd_clear() -> None:
    clear_chat_envelope()
    print("envelope cleared")


async def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    start_p = sub.add_parser("start")
    start_p.add_argument("question_id", nargs="?", default=None)
    sub.add_parser("clear")
    args = parser.parse_args()

    if args.cmd == "status":
        await cmd_status()
    elif args.cmd == "start":
        await cmd_start(args.question_id)
    elif args.cmd == "clear":
        cmd_clear()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
