"""Show, list, or set the active rumil workspace for this Claude Code session.

Usage:
    # Show current + list all
    uv run python .claude/lib/rumil_skills/workspace.py

    # Set current workspace for this session
    uv run python .claude/lib/rumil_skills/workspace.py set <name>

    # List only
    uv run python .claude/lib/rumil_skills/workspace.py list
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from ._runctx import (
    SessionState,
    load_session_state,
    make_db,
    save_session_state,
)


async def _list(current: str) -> None:
    db, _ = await make_db()
    try:
        projects = await db.list_projects()
    finally:
        await db.close()
    if not projects:
        print("no workspaces yet (default will be created on first use)")
        return
    print(f"current: {current}")
    print()
    for p in projects:
        marker = "*" if p.name == current else " "
        created = p.created_at.strftime("%Y-%m-%d")
        print(f"  {marker} {p.name:<20} ({created})")


async def _set(name: str) -> None:
    state = load_session_state()
    prev = state.workspace
    state.workspace = name
    save_session_state(state)
    print(f"workspace: {prev} → {name}")


async def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("list")
    set_parser = sub.add_parser("set")
    set_parser.add_argument("name")
    args = parser.parse_args()

    state = load_session_state()

    if args.cmd == "set":
        await _set(args.name)
        return

    if args.cmd == "list":
        await _list(state.workspace)
        return

    # Default: show current + list
    await _list(state.workspace)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
