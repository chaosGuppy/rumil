"""Embedding-based search over the active rumil workspace.

Returns the top-N most similar pages (questions, claims, judgements, concepts)
to a free-text query, rendered compactly for Claude Code context.

Usage:
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.search_workspace "is the sky blue"
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.search_workspace "..." --limit 12
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from rumil.context import build_embedding_based_context

from ._runctx import make_db


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("query", nargs="+", help="Free-text search query")
    parser.add_argument("--workspace", default=None)
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Approximate limit (truncates the rendered context block)",
    )
    args = parser.parse_args()

    query = " ".join(args.query)

    db, ws = await make_db(workspace=args.workspace)
    try:
        result = await build_embedding_based_context(query, db)
    finally:
        await db.close()

    print(f"workspace: {ws}")
    print(f"query:     {query}")
    print()
    text = result.context_text.rstrip() if result.context_text else "(no matches)"
    if args.limit:
        lines = text.splitlines()
        # Heuristic: ~5 lines per item
        text = "\n".join(lines[: args.limit * 5])
    print(text)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
