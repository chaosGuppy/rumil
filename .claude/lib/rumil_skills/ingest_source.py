"""Commit a source to the workspace and run extraction calls against a question.

This is the *mutating* lane for sources. If you just want to see a source
in the conversation, or stash it without extraction, use ``rumil-read``.

Two input forms:
    1. ``rumil-ingest <file_or_url> --for <q>``
       Fetches + creates a new Source page, then runs ingest rounds.
    2. ``rumil-ingest --from-page <page_id> --for <q>``
       Reuses an already-persisted Source page (e.g. one created by
       ``rumil-read --save`` earlier, or a previous ingest against another
       question). Skips the fetch.

Usage:
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.ingest_source \\
        <file_or_url> --for <q_id> [--budget N] [--smoke-test]
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.ingest_source \\
        --from-page <page_id> --for <q_id> [--budget N] [--smoke-test]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os.path
import sys

from rumil.database import DB
from rumil.models import Call, CallType, Page
from rumil.orchestrators import ingest_until_done
from rumil.settings import get_settings
from rumil.sources import create_source_page

from ._format import print_event, print_trace, truncate
from ._runctx import make_db, open_run

DEFAULT_BUDGET = 1


def _is_url(value: str) -> bool:
    return value.startswith("http://") or value.startswith("https://")


def _looks_like_page_id(value: str) -> bool:
    """True for 8-char hex short IDs or full UUIDs."""
    hex_chars = set("0123456789abcdef")
    v = value.strip().lower()
    if len(v) == 8 and all(c in hex_chars for c in v):
        return True
    if len(v) == 36 and v.count("-") == 4:
        stripped = v.replace("-", "")
        return len(stripped) == 32 and all(c in hex_chars for c in stripped)
    return False


async def _get_or_create_source(
    source_arg: str | None,
    from_page: str | None,
    db: DB,
) -> Page | None:
    """Resolve an existing source page or create a new one. Prints status lines."""
    if from_page is not None:
        full_id = await db.resolve_page_id(from_page)
        if not full_id:
            print(f"no page matching {from_page!r}")
            return None
        page = await db.get_page(full_id)
        if page is None:
            print(f"page {full_id[:8]} vanished mid-lookup")
            return None
        print_event("•", f"reusing source {full_id[:8]}  {truncate(page.headline, 70)}")
        return page

    assert source_arg is not None
    kind = "url" if _is_url(source_arg) else "file"
    print_event("•", f"creating source from {kind}: {source_arg}")
    page = await create_source_page(source_arg, db)
    return page


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "source",
        nargs="?",
        help="File path or http(s):// URL (omit if using --from-page)",
    )
    parser.add_argument(
        "--from-page",
        dest="from_page",
        help="Reuse an existing Source page by full or short (8-char) ID",
    )
    parser.add_argument(
        "--for",
        dest="for_question",
        required=True,
        help="Question ID to extract considerations for (full or short)",
    )
    parser.add_argument("--workspace", default=None)
    parser.add_argument("--budget", type=int, default=DEFAULT_BUDGET)
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="Faster/cheaper model, fewer rounds (for testing)",
    )
    args = parser.parse_args()

    if not args.source and not args.from_page:
        print("error: must pass either <source> or --from-page <id>")
        sys.exit(2)
    if args.source and args.from_page:
        print("error: pass either <source> or --from-page, not both")
        sys.exit(2)

    if (
        args.source
        and not args.from_page
        and not _is_url(args.source)
        and not os.path.exists(args.source)
        and _looks_like_page_id(args.source)
    ):
        args.from_page = args.source
        args.source = None

    if args.smoke_test:
        get_settings().rumil_smoke_test = "1"

    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s %(name)s %(message)s",
    )

    db, ws = await make_db(workspace=args.workspace)
    try:
        question_id = await db.resolve_page_id(args.for_question)
        if not question_id:
            print(f"no question matching {args.for_question!r} in workspace {ws!r}")
            sys.exit(1)
        question = await db.get_page(question_id)
        if question is None:
            print(f"question {question_id[:8]} vanished mid-lookup")
            sys.exit(1)
        if question.project_id and question.project_id != db.project_id:
            db.project_id = question.project_id

        print(f"workspace: {ws}")
        print(f"question:  {question_id[:8]}  {truncate(question.headline, 80)}")

        await open_run(
            db,
            name=f"ingest → {question.headline}",
            question_id=question_id,
            skill="rumil-ingest",
            budget=args.budget,
            extra_config={
                "smoke_test": bool(args.smoke_test),
                "source_input": "page_id"
                if args.from_page
                else ("url" if _is_url(args.source or "") else "file"),
            },
        )
        print_trace(db.run_id)

        source_page = await _get_or_create_source(args.source, args.from_page, db)
        if source_page is None:
            sys.exit(1)

        print_event(
            "→",
            f"extracting considerations from {source_page.id[:8]} (budget {args.budget})",
        )
        rounds = await ingest_until_done(source_page, question_id, db)
        total, used = await db.get_budget()

        # Pull the most recent INGEST call against this source/run for its summary.
        latest = await _latest_ingest_call(db, source_page.id)
        cost_s = (
            f"${latest.cost_usd:.3f}" if latest is not None and latest.cost_usd is not None else "—"
        )
        status = latest.status.value if latest is not None else "no-calls"
        print_event(
            "✓",
            f"done: {rounds} round{'s' if rounds != 1 else ''} status={status} "
            f"cost={cost_s} budget={used}/{total}",
        )
        if latest is not None and latest.result_summary:
            print()
            print(latest.result_summary.rstrip())
    finally:
        await db.close()


async def _latest_ingest_call(db: DB, source_page_id: str) -> Call | None:
    """Return the most recent INGEST call for this run scoped to the source."""
    resp = await db._execute(
        db.client.table("calls")
        .select("*")
        .eq("run_id", db.run_id)
        .eq("call_type", CallType.INGEST.value)
        .eq("scope_page_id", source_page_id)
        .order("created_at", desc=True)
        .limit(1)
    )
    rows = getattr(resp, "data", None) or []
    if not rows:
        return None
    return Call.model_validate(rows[0])


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
