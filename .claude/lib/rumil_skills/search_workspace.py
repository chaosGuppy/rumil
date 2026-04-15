"""Embedding-based search over the active rumil workspace.

Two modes:

- **quick** (default): top-N pages at ABSTRACT detail with similarity scores.
  Fast, cheap, designed for mid-conversation lookups when Claude wants to
  check what the workspace already knows about a topic.
- **full** (``--full``): the multi-tier context builder used inside real
  rumil calls. Produces a richer, rendered context block (full pages +
  summaries) within the configured char budgets. Use when the user wants
  a deeper read, not just a pointer list.

Usage:
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.search_workspace "query"
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.search_workspace "query" --limit 12
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.search_workspace "query" --full
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from rumil.context import build_embedding_based_context, format_page
from rumil.embeddings import search_pages
from rumil.models import PageDetail

from ._runctx import make_db


async def _run_quick(db, ws: str, query: str, limit: int) -> None:
    results = await search_pages(db, query, match_threshold=0.3, match_count=limit)

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


async def _run_full(db, ws: str, query: str, limit: int | None) -> None:
    result = await build_embedding_based_context(query, db)

    print(f"workspace: {ws}")
    print(f"query:     {query}")
    print()
    text = result.context_text.rstrip() if result.context_text else "(no matches)"
    if limit:
        lines = text.splitlines()
        text = "\n".join(lines[: limit * 5])
    print(text)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("query", nargs="+", help="Free-text search query")
    parser.add_argument("--workspace", default=None)
    parser.add_argument(
        "--full",
        action="store_true",
        help="Use the full multi-tier context builder instead of the quick top-N mode",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help=(
            "In quick mode: number of results (default 8). "
            "In --full mode: approximate cap via truncating the rendered block."
        ),
    )
    args = parser.parse_args()

    query = " ".join(args.query)

    db, ws = await make_db(workspace=args.workspace)
    try:
        if args.full:
            await _run_full(db, ws, query, args.limit)
        else:
            await _run_quick(db, ws, query, args.limit or 8)
    finally:
        await db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
