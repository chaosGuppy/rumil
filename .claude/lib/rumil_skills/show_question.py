"""Show a rumil question: subtree render + embedding neighbors + recent calls.

Usage:
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.show_question <qid>
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.show_question <qid> --depth 3 --no-neighbors
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from rumil.context import build_embedding_based_context
from rumil.summary import build_research_tree

from ._format import truncate
from ._runctx import make_db
from .scan import collect_subtree, format_compact, graph_health, rating_shape


async def _recent_calls(db, question_id: str, limit: int = 8) -> list[dict]:
    """Fetch recent calls scoped to this question (direct DB query)."""
    rows = await db._execute(
        db.client.table("calls")
        .select("id,call_type,status,cost_usd,created_at")
        .eq("scope_page_id", question_id)
        .order("created_at", desc=True)
        .limit(limit)
    )
    data = getattr(rows, "data", None) or []
    return list(data)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("question_id", help="Full or short (8-char) question ID")
    parser.add_argument("--workspace", default=None)
    parser.add_argument("--depth", type=int, default=3)
    parser.add_argument("--no-neighbors", action="store_true")
    parser.add_argument("--no-calls", action="store_true")
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

        # Subtree
        print("=== research subtree ===")
        tree = await build_research_tree(full_id, db, max_depth=args.depth)
        print(tree.rstrip())
        print()

        # Shape summary (compact)
        scan_data = await collect_subtree(db, full_id)
        findings = graph_health(scan_data) + rating_shape(scan_data)
        compact = format_compact(findings)
        print(f"shape: {compact}")
        print()

        # Embedding neighbors
        if not args.no_neighbors:
            print("=== embedding neighbors ===")
            result = await build_embedding_based_context(
                question.content or question.headline,
                db,
                scope_question_id=full_id,
            )
            ctx_text = getattr(result, "context_text", "")
            print(ctx_text.rstrip() if ctx_text else "(empty)")
            print()

        # Recent calls
        if not args.no_calls:
            print("=== recent calls on this question ===")
            calls = await _recent_calls(db, full_id)
            if not calls:
                print("(none)")
            else:
                for c in calls:
                    short = c["id"][:8]
                    ctype = c.get("call_type", "?")
                    status = c.get("status", "?")
                    cost = c.get("cost_usd")
                    cost_s = f"${cost:.3f}" if cost is not None else "     "
                    created = (c.get("created_at") or "")[:19].replace("T", " ")
                    print(f"  {short}  {created}  {status:8}  {cost_s}  {ctype}")
    finally:
        await db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
