"""Run the rumil orchestrator against an existing question from Claude Code.

This is the CC-initiated equivalent of ``main.py --continue <id> --budget N``
and backs the ``/rumil-orchestrate`` skill. The orchestrator dispatches a
*sequence* of calls (prioritize, scout, find-considerations, assess, etc.)
until the budget is consumed. This is the multi-call sibling of
``/rumil-dispatch`` — use this when the user wants real research done, not
a single targeted call.

Unlike ``/rumil-dispatch`` (which uses the default staged=True), this runs
with ``staged=False`` so the resulting pages are visible in the baseline
workspace immediately, the same way a ``main.py`` run would be.

Usage:
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.run_orchestrator \\
        <question_id> [--budget N] [--smoke-test] [--workspace NAME] \\
        [--name TEXT]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from rumil.models import PageType
from rumil.orchestrators import Orchestrator
from rumil.settings import get_settings

from ._format import print_event, print_trace, truncate
from ._runctx import make_db, open_run

DEFAULT_BUDGET = 10
ORCHESTRATOR_CHOICES = ("two_phase", "experimental")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "question_id",
        help="Full or short (8-char) question ID",
    )
    parser.add_argument(
        "--budget",
        type=int,
        default=DEFAULT_BUDGET,
        help=f"Research call budget (default: {DEFAULT_BUDGET})",
    )
    parser.add_argument("--workspace", default=None)
    parser.add_argument(
        "--orchestrator",
        choices=ORCHESTRATOR_CHOICES,
        default=None,
        help=(
            "Which research-loop orchestrator to run. Sets "
            "settings.prioritizer_variant for this invocation. Defaults to "
            "whatever the settings have (typically 'two_phase')."
        ),
    )
    parser.add_argument(
        "--global-prio",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Force settings.enable_global_prio on or off for this invocation "
            "(overrides the ENABLE_GLOBAL_PRIO env var / .env default). "
            "When enabled, GlobalPrioOrchestrator wraps the chosen prioritizer "
            "variant. Omit to inherit the env/settings default."
        ),
    )
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="Faster/cheaper model, fewer rounds (for testing)",
    )
    parser.add_argument(
        "--name",
        default="",
        help="Optional run name (defaults to question headline)",
    )
    args = parser.parse_args()

    if args.smoke_test:
        get_settings().rumil_smoke_test = "1"
    if args.orchestrator:
        get_settings().prioritizer_variant = args.orchestrator
    if args.global_prio is not None:
        get_settings().enable_global_prio = args.global_prio

    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s %(name)s %(message)s",
    )

    # staged=False so orchestrator output is visible to the baseline
    # workspace the same way main.py --continue would leave it.
    db, ws = await make_db(workspace=args.workspace, staged=False)
    try:
        full_id = await db.resolve_page_id(args.question_id)
        if not full_id:
            print(f"no question matching {args.question_id!r} in workspace {ws!r}")
            sys.exit(1)
        page = await db.get_page(full_id)
        if page is None:
            print(f"page {full_id[:8]} vanished mid-lookup")
            sys.exit(1)
        if page.page_type != PageType.QUESTION:
            print(f"error: page {full_id[:8]} is a {page.page_type.value}, not a question")
            sys.exit(1)
        if page.project_id and page.project_id != db.project_id:
            db.project_id = page.project_id

        settings = get_settings()
        variant = settings.prioritizer_variant
        global_prio = settings.enable_global_prio
        print(f"workspace:    {ws}")
        print(f"question:     {full_id[:8]}  {truncate(page.headline, 80)}")
        print(f"orchestrator: {variant}{' (+global_prio)' if global_prio else ''}")

        await open_run(
            db,
            name=args.name or page.headline,
            question_id=full_id,
            skill="rumil-orchestrate",
            budget=args.budget,
            extra_config={"smoke_test": bool(args.smoke_test)},
        )
        print_trace(db.run_id)

        print_event("→", f"running {variant} orchestrator (budget {args.budget})")
        await Orchestrator(db).run(full_id)

        total, used = await db.get_budget()
        print_event("✓", f"done: budget={used}/{total}")
    finally:
        await db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
