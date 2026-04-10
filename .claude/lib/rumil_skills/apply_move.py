"""Apply one rumil move from Claude Code context onto the chat envelope.

This is the *cc-mediated* lane: Claude Code is the brain, deciding from
its broader conversation context that a specific move should happen.
The move is applied directly (no rumil-internal LLM call) and is owned
by the CLAUDE_CODE_DIRECT envelope Call. Its presence in the trace — and
the envelope's call_type — make the provenance unambiguous.

By contrast, /rumil-dispatch fires a *full* rumil call where the rumil
prompt and tools decide what moves to make.

Usage:
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.apply_move \\
        <move_type> '<payload_json>'

    # Example: add a subquestion under parent Q#abc12345
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.apply_move \\
        CREATE_QUESTION '{"headline": "What are X's second-order effects?",
                          "content": "Explore downstream..."}'

    # Then link it as a child
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.apply_move \\
        LINK_CHILD_QUESTION '{"parent_id": "abc12345", "child_id": "def67890"}'

Move types are the string values in rumil.models.MoveType. Use
'--list' to see them:
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.apply_move --list
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from rumil.models import MoveType
from rumil.moves.registry import MOVES

from ._format import print_event, print_trace
from ._runctx import ensure_chat_envelope


def _list_moves() -> None:
    print("Available moves:")
    for mt in MoveType:
        move_def = MOVES.get(mt)
        if move_def is None:
            continue
        desc = (move_def.description or "").splitlines()[0][:100]
        print(f"  {mt.value:<24} {desc}")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "move_type",
        nargs="?",
        help="Move type (e.g. CREATE_QUESTION). See --list.",
    )
    parser.add_argument(
        "payload",
        nargs="?",
        help="JSON payload matching the move's schema",
    )
    parser.add_argument("--list", action="store_true", help="List available moves")
    parser.add_argument(
        "--scope",
        default=None,
        help="Optional scope question id (used when creating a new envelope)",
    )
    args = parser.parse_args()

    if args.list:
        _list_moves()
        return

    if not args.move_type or not args.payload:
        parser.error("move_type and payload are required (or use --list)")

    try:
        move_type = MoveType(args.move_type)
    except ValueError:
        print(f"unknown move type: {args.move_type!r}. Use --list.", file=sys.stderr)
        sys.exit(2)

    move_def = MOVES.get(move_type)
    if move_def is None:
        print(f"move {move_type} has no MoveDef in the registry", file=sys.stderr)
        sys.exit(2)

    try:
        payload_dict = json.loads(args.payload)
    except json.JSONDecodeError as e:
        print(f"invalid JSON payload: {e}", file=sys.stderr)
        sys.exit(2)

    try:
        validated = move_def.schema(**payload_dict)
    except Exception as e:
        print(f"payload validation failed: {e}", file=sys.stderr)
        sys.exit(2)

    db, call = await ensure_chat_envelope(scope_question_id=args.scope)
    try:
        print(f"envelope:  call={call.id[:8]} run={db.run_id[:8]}")
        print_trace(db.run_id, label="trace url")
        print_event("⚙", f"cc-mediated move: {move_type.value}")
        result = await move_def.execute(validated, call, db)
        if result.created_page_id:
            print_event("•", f"created page {result.created_page_id[:8]}")
        if result.extra_created_ids:
            for pid in result.extra_created_ids:
                print_event("•", f"also created {pid[:8]}")
        print()
        print(result.message.rstrip())
    finally:
        await db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
