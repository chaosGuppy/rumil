"""Lightweight workspace search for Claude Code mid-conversation use.

Modeled on chat.py's search_workspace tool: returns top-N pages at ABSTRACT
detail with similarity scores. Much cheaper than the full context builder
used by /rumil-search — designed to be called via Bash when Claude needs a
quick check of what the workspace already knows about a topic.

Usage:
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.quick_search "query"
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.quick_search "query" --limit 12
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from rumil.context import format_page
from rumil.embeddings import search_pages
from rumil.models import PageDetail

from ._runctx import make_db


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("query", nargs="+", help="Free-text search query")
    parser.add_argument("--workspace", default=None)
    parser.add_argument(
        "--limit", type=int, default=8, help="Number of results (default: 8)"
    )
    args = parser.parse_args()

    query = " ".join(args.query)

    db, ws = await make_db(workspace=args.workspace)
    try:
        results = await search_pages(
            db, query, match_threshold=0.3, match_count=args.limit
        )
    finally:
        await db.close()

    if not results:
        print(f"workspace: {ws}")
        print(f"query:     {query}")
        print("\nNo matching pages found.")
        return

    print(f"workspace: {ws} | {len(results)} results for: {query}\n")
    parts: list[str] = []
    for page, score in results:
        formatted = await format_page(page, PageDetail.ABSTRACT, db=None)
        parts.append(f"(similarity: {score:.2f})\n{formatted}")
    print("\n---\n".join(parts))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
