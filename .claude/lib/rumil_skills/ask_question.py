"""Pose a new research question to the active rumil workspace.

This is the *cc-mediated* lane for question creation: the new question
page is owned by the current CC session's CLAUDE_CODE_DIRECT envelope
Call, so the provenance is unambiguous in the trace.

This script does **not** run any research calls. After it returns, chain
with ``/rumil-run <id>`` (full orchestrator) or ``/rumil-dispatch
<call_type> <id>`` (single call) to investigate.

Usage:
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.ask_question \\
        "<headline>" [--parent <qid>] [--abstract "..."] [--content "..."] \\
        [--workspace NAME]

    # Or with a .json file for structured input:
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.ask_question \\
        path/to/question.json [--parent <qid>]

The .json file must have ``headline`` and optionally ``abstract`` and
``content``. CLI flags override fields loaded from JSON.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

from rumil.embeddings import embed_and_store_page
from rumil.models import (
    LinkType,
    MoveType,
    Page,
    PageLayer,
    PageLink,
    PageType,
    Workspace,
)
from rumil.moves.base import write_page_file
from rumil.tracing.trace_events import (
    MoveTraceItem,
    MovesExecutedEvent,
    PageRef,
)
from rumil.tracing.tracer import CallTrace

from ._format import print_event, print_trace, truncate
from ._runctx import ensure_chat_envelope, resolve_workspace


@dataclass
class QuestionInput:
    headline: str
    abstract: str = ""
    content: str = ""


def parse_question_input(value: str) -> QuestionInput:
    """Plain text → headline-only. .json path → structured fields.

    Mirrors the behaviour of ``main.py``'s ``parse_question_input`` so
    JSON files written for either surface are interchangeable.
    """
    path = Path(value)
    if path.suffix == ".json":
        if not path.exists():
            sys.exit(f"error: question JSON file not found: {value}")
        with open(path) as f:
            data = json.load(f)
        if not isinstance(data, dict) or "headline" not in data:
            sys.exit('error: JSON file must contain at least a "headline" field')
        unknown = set(data) - {"headline", "abstract", "content"}
        if unknown:
            sys.exit(
                f"error: unknown fields in question JSON: {', '.join(sorted(unknown))}"
            )
        return QuestionInput(
            headline=data["headline"],
            abstract=data.get("abstract", ""),
            content=data.get("content", ""),
        )
    return QuestionInput(headline=value)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "question",
        help=(
            "Headline text (plain string) or path to a .json file with "
            "headline/abstract/content fields"
        ),
    )
    parser.add_argument(
        "--parent",
        default=None,
        metavar="QID",
        help=(
            "Parent question ID (full or short 8-char). Makes this a "
            "sub-question of that parent."
        ),
    )
    parser.add_argument(
        "--abstract",
        default=None,
        help="1-3 sentence summary. Overrides abstract from .json input.",
    )
    parser.add_argument(
        "--content",
        default=None,
        help=(
            "Longer description. Overrides content from .json input. "
            "Defaults to the headline when neither is set."
        ),
    )
    parser.add_argument("--workspace", default=None)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s %(name)s %(message)s",
    )

    q = parse_question_input(args.question)
    if args.abstract is not None:
        q.abstract = args.abstract
    if args.content is not None:
        q.content = args.content

    ws = resolve_workspace(args.workspace)
    db, envelope = await ensure_chat_envelope(workspace=args.workspace)
    try:
        parent_resolved: str | None = None
        if args.parent:
            parent_resolved = await db.resolve_page_id(args.parent)
            if not parent_resolved:
                print(
                    f"error: no question matching {args.parent!r} in workspace {ws!r}"
                )
                sys.exit(1)
            parent_page = await db.get_page(parent_resolved)
            if parent_page is None:
                print(f"error: parent {parent_resolved[:8]} vanished mid-lookup")
                sys.exit(1)
            if parent_page.page_type != PageType.QUESTION:
                print(
                    f"error: parent {parent_resolved[:8]} is a "
                    f"{parent_page.page_type.value}, not a question"
                )
                sys.exit(1)

        print(f"workspace: {ws}")
        print(f"envelope:  call={envelope.id[:8]} run={db.run_id[:8]}")
        print_trace(db.run_id, label="trace url")

        page = Page(
            page_type=PageType.QUESTION,
            layer=PageLayer.SQUIDGY,
            workspace=Workspace.RESEARCH,
            content=q.content or q.abstract or q.headline,
            headline=q.headline,
            abstract=q.abstract,
            provenance_model="human",
            provenance_call_type=envelope.call_type.value,
            provenance_call_id=envelope.id,
            extra={"status": "open"},
        )
        await db.save_page(page)
        write_page_file(page)
        try:
            await embed_and_store_page(db, page, field_name="abstract")
        except Exception as e:
            print(
                f"  warning: failed to embed new question: {e}",
                file=sys.stderr,
            )

        print_event(
            "•",
            f"created question {page.id[:8]}  {truncate(q.headline, 70)}",
        )

        if parent_resolved:
            link = PageLink(
                from_page_id=parent_resolved,
                to_page_id=page.id,
                link_type=LinkType.CHILD_QUESTION,
                reasoning="Added via /rumil-ask from Claude Code",
            )
            await db.save_link(link)
            print_event("•", f"linked as child of {parent_resolved[:8]}")

        trace_item = MoveTraceItem(
            type=MoveType.CREATE_QUESTION.value,
            headline=truncate(q.headline, 100),
            page_refs=[PageRef(id=page.id, headline=truncate(q.headline, 100))],
        )
        event = MovesExecutedEvent(moves=[trace_item])
        try:
            await CallTrace(envelope.id, db).record(event)
        except Exception as e:
            print(
                f"  warning: failed to record trace event: {e}",
                file=sys.stderr,
            )

        print()
        print(f"question id: {page.id}")
        print(f"to investigate: /rumil-run {page.id[:8]} --budget 10")
    finally:
        await db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
