"""Show a single rumil page by short ID — full content, provenance, links.

Fills the gap where `show_question` is question-specific: when you see
a claim, judgement, concept, or wiki-page short ID in a trace or punch
list and want to read its full content without switching to the
frontend, this is the skill to use.

Usage:
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.show_page <page_id>
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.show_page <page_id> --no-links
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from ._format import truncate
from ._runctx import make_db


def _fmt_scalar(value: object) -> str:
    if value is None:
        return "—"
    return str(value)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("page_id", help="Full or short (8-char) page ID")
    parser.add_argument("--workspace", default=None)
    parser.add_argument(
        "--no-links",
        action="store_true",
        help="Skip the incoming/outgoing link sections",
    )
    parser.add_argument(
        "--content-limit",
        type=int,
        default=4000,
        help="Truncate the content body at this many chars (0 = no limit)",
    )
    args = parser.parse_args()

    db, ws = await make_db(workspace=args.workspace)
    try:
        full_id = await db.resolve_page_id(args.page_id)
        if not full_id:
            print(f"no page matching {args.page_id!r} in workspace {ws!r}")
            sys.exit(1)
        page = await db.get_page(full_id)
        if page is None:
            print(f"page {full_id[:8]} vanished mid-lookup")
            sys.exit(1)

        print(f"workspace: {ws}")
        print(f"id:        {full_id}  ({full_id[:8]})")
        print(f"type:      {page.page_type.value}")
        print(f"layer:     {page.layer.value}")
        print(f"page_ws:   {page.workspace.value}")
        print(f"headline:  {page.headline}")
        print(f"created:   {page.created_at.isoformat()[:19].replace('T', ' ')}")
        print(
            f"provenance: model={page.provenance_model or '—'}  "
            f"call_type={page.provenance_call_type or '—'}  "
            f"call_id={(page.provenance_call_id or '—')[:8]}"
        )
        if page.credence is not None or page.robustness is not None:
            print(
                f"epistemic: credence={_fmt_scalar(page.credence)}  "
                f"robustness={_fmt_scalar(page.robustness)}"
            )
        if page.is_superseded:
            print(f"SUPERSEDED by {(page.superseded_by or '—')[:8]}")
        if page.fruit_remaining is not None:
            print(f"fruit_remaining: {page.fruit_remaining}")
        if page.extra:
            print(f"extra:     {truncate(str(page.extra), 200)}")

        if page.abstract:
            print()
            print("=== abstract ===")
            print(page.abstract.rstrip())

        if page.content:
            print()
            print("=== content ===")
            content = page.content.rstrip()
            if args.content_limit and len(content) > args.content_limit:
                print(content[: args.content_limit])
                print(
                    f"… [truncated at {args.content_limit} chars; --content-limit 0 for full]"
                )
            else:
                print(content)

        if args.no_links:
            return

        outgoing = await db.get_links_from(full_id)
        incoming = await db.get_links_to(full_id)

        related_ids: list[str] = []
        for link in outgoing:
            related_ids.append(link.to_page_id)
        for link in incoming:
            related_ids.append(link.from_page_id)
        related_ids = list(dict.fromkeys(related_ids))  # de-dup, preserve order
        related_pages = await db.get_pages_by_ids(related_ids) if related_ids else {}

        def _label(pid: str) -> str:
            p = related_pages.get(pid)
            if p is None:
                return f"{pid[:8]} (missing)"
            return f"{pid[:8]} {truncate(p.headline, 70)}"

        print()
        print("=== outgoing links ===")
        if not outgoing:
            print("(none)")
        else:
            for link in outgoing:
                reasoning = (
                    f"  reasoning: {truncate(link.reasoning, 120)}"
                    if link.reasoning
                    else ""
                )
                print(
                    f"  {link.link_type.value:16} → {_label(link.to_page_id)}"
                    f"  [role={link.role.value} strength={link.strength}]" + reasoning
                )

        print()
        print("=== incoming links ===")
        if not incoming:
            print("(none)")
        else:
            for link in incoming:
                reasoning = (
                    f"  reasoning: {truncate(link.reasoning, 120)}"
                    if link.reasoning
                    else ""
                )
                print(
                    f"  {link.link_type.value:16} ← {_label(link.from_page_id)}"
                    f"  [role={link.role.value} strength={link.strength}]" + reasoning
                )
    finally:
        await db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
