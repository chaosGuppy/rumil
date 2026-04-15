"""Fetch a file or URL into the conversation — the *view-only* lane for sources.

Default mode is pure fetch-and-print: no DB connection, no LLM call. Uses the
same extraction pipeline that ``rumil.sources.create_source_page`` uses, so
what you see here is byte-identical to what ``rumil-ingest`` would feed the
extraction step.

Flags layer additional behavior:
    --summary   also generate the 2-3 sentence LLM headline that a Source page
                would get. Still no DB.
    --save      persist as a PageType.SOURCE page in the active workspace.
                Implies a summary (create_source_page always summarizes).
                This is the only path that touches the DB.

Usage:
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.read_source <source>
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.read_source <source> --save
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.read_source <source> --summary
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.read_source <source> --full
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from rumil.scraper import ScrapedPage, scrape_url
from rumil.sources import (
    INGEST_MAX_CHARS,
    generate_source_summary,
    read_file_content,
)

from ._format import print_event, truncate
from ._runctx import resolve_workspace

DISPLAY_LIMIT = 50_000


def _is_url(value: str) -> bool:
    return value.startswith("http://") or value.startswith("https://")


async def _fetch_url(url: str) -> ScrapedPage | None:
    print_event("•", f"fetching {url}")
    return await scrape_url(url, max_chars=INGEST_MAX_CHARS)


def _fetch_file(filepath: str) -> tuple[str, str] | None:
    """Returns (label, content) or None on error."""
    path = Path(filepath)
    if not path.exists():
        print(f"error: file not found: {filepath}")
        return None
    try:
        content = read_file_content(path)
    except Exception as e:
        print(f"error reading {filepath}: {e}")
        return None
    print_event("•", f"read {path.name} ({len(content):,} chars)")
    return path.name, content


def _print_content(content: str, full: bool) -> None:
    """Print content with a soft truncation default so we don't blow out context."""
    print()
    print("=== content ===")
    if full or len(content) <= DISPLAY_LIMIT:
        print(content.rstrip())
        return
    print(content[:DISPLAY_LIMIT].rstrip())
    print(
        f"\n… [truncated at {DISPLAY_LIMIT:,} chars of {len(content):,}; "
        "pass --full for the whole thing]"
    )


async def _do_save(
    source_arg: str,
    workspace: str | None,
) -> None:
    """Delegate to create_source_page, which handles fetch + summary + persist."""
    # Local imports: DB-touching path, keep the default lane import-light.
    from rumil.sources import create_source_page

    from ._runctx import make_db

    db, ws = await make_db(workspace=workspace)
    try:
        print(f"workspace: {ws}")
        print()
        page = await create_source_page(source_arg, db)
        if page is None:
            sys.exit(1)
        print_event("✓", f"saved source {page.id[:8]}  ({page.id})")
        if page.headline:
            print(f"headline:  {truncate(page.headline, 200)}")
        print()
        print("=== content ===")
        content = page.content or ""
        if len(content) <= DISPLAY_LIMIT:
            print(content.rstrip())
        else:
            print(content[:DISPLAY_LIMIT].rstrip())
            print(
                f"\n… [truncated at {DISPLAY_LIMIT:,} chars of {len(content):,}; "
                "the full content is persisted on the source page]"
            )
    finally:
        await db.close()


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", help="File path or http(s):// URL")
    parser.add_argument(
        "--save",
        action="store_true",
        help="Also persist as a PageType.SOURCE page (requires DB; auto-summarizes)",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Generate a 2-3 sentence LLM headline (no DB; redundant with --save)",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help=f"Print the full fetched content (default truncates display at {DISPLAY_LIMIT:,} chars)",
    )
    parser.add_argument(
        "--workspace",
        default=None,
        help="Override the session workspace (defaults to session state)",
    )
    args = parser.parse_args()

    if args.save:
        await _do_save(args.source, args.workspace)
        return

    # Pure fetch-and-print path: no DB, but still resolve the workspace so the
    # output header is consistent with other rumil-* skills.
    ws = resolve_workspace(args.workspace)
    title: str
    content: str
    extra_lines: list[str] = []

    if _is_url(args.source):
        scraped = await _fetch_url(args.source)
        if scraped is None:
            print(f"error: failed to fetch {args.source}")
            sys.exit(1)
        title = scraped.title
        content = scraped.content
        extra_lines.append(f"url:       {scraped.url}")
        extra_lines.append(f"fetched:   {scraped.fetched_at}")
    else:
        fetched = _fetch_file(args.source)
        if fetched is None:
            sys.exit(1)
        title, content = fetched

    print(f"workspace: {ws}")
    print(f"source:    {title}")
    for line in extra_lines:
        print(line)
    print(f"chars:     {len(content):,}")

    if args.summary:
        print_event("•", "generating summary")
        summary = await generate_source_summary(content, title)
        print(f"summary:   {truncate(summary, 300)}")

    _print_content(content, full=args.full)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
