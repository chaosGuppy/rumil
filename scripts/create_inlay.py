"""Persist a hand-authored Inlay page bound to a question.

An Inlay is a model-authored (or for MVP, hand-authored) HTML+CSS+JS
blob that replaces the stock content area for a question with a
sandboxed iframe. See planning/inlay-ui.md for the design.

This is the Phase 1 (MVP) authoring path — a helper that takes a path
to a standalone HTML file and an 8-char or full question ID, and
writes an INLAY page plus an INLAY_OF link.

Usage:

    uv run python scripts/create_inlay.py \\
        --question-id <qid> --source examples/inlays/forecast-card.html \\
        --workspace my-workspace

The question-id can be a full UUID or an 8-char short prefix. The
workspace (project) must already exist and contain the question. Pass
``--headline "..."`` to override the default headline (derived from
the HTML ``<title>`` tag or the source file stem).

The run_id we create is only used to tag the created rows; this
command runs in non-staged mode so the new Inlay is immediately
visible to other readers. To shadow an author experiment, pass
``--staged`` (the run_id will be printed so you can ``db.stage_run``
it later if you want to promote or revert).
"""

import argparse
import asyncio
import logging
import re
import sys
import uuid
from pathlib import Path

from rumil.database import DB
from rumil.models import (
    LinkType,
    Page,
    PageLayer,
    PageLink,
    PageType,
    Workspace,
)

log = logging.getLogger(__name__)


_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def _derive_headline(source: str, fallback: str) -> str:
    match = _TITLE_RE.search(source)
    if match:
        text = match.group(1).strip()
        if text:
            return text[:200]
    return fallback


async def create_inlay(
    question_id: str,
    source_path: Path,
    workspace: str,
    headline: str | None,
    author: str,
    staged: bool,
) -> None:
    if not source_path.exists():
        raise FileNotFoundError(f"Source file not found: {source_path}")
    source = source_path.read_text(encoding="utf-8")
    if not source.strip():
        raise ValueError(f"Source file is empty: {source_path}")

    run_id = str(uuid.uuid4())
    db = await DB.create(run_id=run_id, staged=staged)
    try:
        project, created = await db.get_or_create_project(workspace)
        if created:
            log.warning(
                "Workspace '%s' did not exist — created it. "
                "Double-check the name is what you intended.",
                workspace,
            )
        db.project_id = project.id

        resolved_qid = await db.resolve_page_id(question_id)
        if not resolved_qid:
            raise ValueError(
                f"Question {question_id!r} not found in workspace '{workspace}'. "
                "Check the id and --workspace flag."
            )
        question = await db.get_page(resolved_qid)
        if not question:
            raise ValueError(f"Question {resolved_qid} not found.")
        if question.page_type != PageType.QUESTION:
            raise ValueError(
                f"Target page {resolved_qid} is a {question.page_type.value}, "
                "not a question — Inlays only bind to questions in the MVP."
            )

        default_headline = f"Inlay: {source_path.stem}"
        derived_headline = headline or _derive_headline(source, default_headline)

        abstract = (
            f"Hand-authored inlay for question {resolved_qid[:8]} ({question.headline[:80]})."
        )

        inlay = Page(
            page_type=PageType.INLAY,
            layer=PageLayer.SQUIDGY,
            workspace=Workspace.RESEARCH,
            content=source,
            headline=derived_headline,
            abstract=abstract,
            project_id=project.id,
            provenance_model="human",
            provenance_call_type="",
            provenance_call_id="",
            extra={
                "target_id": resolved_qid,
                "author_kind": author,
                "api_version": "rumil.inlay.v1",
                "source_path": str(source_path),
            },
            run_id=run_id,
        )
        await db.save_page(inlay)

        link = PageLink(
            from_page_id=inlay.id,
            to_page_id=resolved_qid,
            link_type=LinkType.INLAY_OF,
            run_id=run_id,
        )
        await db.save_link(link)

        print(f"Created Inlay {inlay.id} ({inlay.id[:8]})")
        print(f"  workspace: {workspace} ({project.id})")
        print(f"  target:    {resolved_qid[:8]} {question.headline[:80]!r}")
        print(f"  source:    {source_path} ({len(source)} chars)")
        print(f"  staged:    {staged}")
        print(f"  run_id:    {run_id}")
        if staged:
            print(
                "\nStaged=True: this inlay is only visible to readers "
                "scoping to this run_id. Pass it to db.stage_run or "
                "edit it further before it's broadcast to everyone."
            )
        print(
            "\nTo select it, open the question in Parma and set "
            f"localStorage.setItem('parma:inlay:<qid>', '{inlay.id}')."
        )
    finally:
        await db.close()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="Persist an Inlay page bound to a question.")
    parser.add_argument(
        "--question-id",
        required=True,
        help="Full UUID or 8-char short ID of the target question.",
    )
    parser.add_argument(
        "--source",
        required=True,
        type=Path,
        help="Path to the HTML file containing the inlay source.",
    )
    parser.add_argument(
        "--workspace",
        required=True,
        help="Project/workspace name the question belongs to.",
    )
    parser.add_argument(
        "--headline",
        default=None,
        help=(
            "Override the derived headline. Defaults to the HTML "
            "<title> tag or the source file stem."
        ),
    )
    parser.add_argument(
        "--author",
        default="user",
        choices=["user", "call"],
        help=(
            "Origin tag written to extra.author_kind. 'user' for "
            "hand-authored or chat-authored inlays; 'call' for "
            "orchestrator-generated ones (Phase 3)."
        ),
    )
    parser.add_argument(
        "--staged",
        action="store_true",
        help=(
            "Write rows as staged=true so they are invisible to other "
            "readers until the run is promoted. Off by default — the "
            "MVP expects inlays to land baseline."
        ),
    )
    args = parser.parse_args(argv)
    try:
        asyncio.run(
            create_inlay(
                question_id=args.question_id,
                source_path=args.source,
                workspace=args.workspace,
                headline=args.headline,
                author=args.author,
                staged=args.staged,
            )
        )
    except (FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
