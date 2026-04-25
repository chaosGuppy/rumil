"""Run the generative (generator-refiner) workflow end-to-end against the local DB.

Usage:

    uv run python scripts/run_generative.py \
        "Write a one-page brief on whether we should adopt X." \
        --workspace generative-scratch \
        --budget 25

    # With an explicit headline (otherwise the first 120 chars of the request are used)
    uv run python scripts/run_generative.py "<long request>" \
        --headline "Adopt X? one-page brief" --workspace generative-scratch --budget 25

    # Resume an interrupted run. Give it the task id printed by the earlier run.
    uv run python scripts/run_generative.py --resume <task-id> --budget 15

    # Run against production (be careful)
    uv run python scripts/run_generative.py "..." --prod --workspace my-project --budget 25

The script prints the trace URL before starting and, when the orchestrator
finishes, prints the artefact headline + content so you can see what came out
without opening the frontend. The task question stays hidden, so nothing leaks
into the default workspace view unless you pass --include-hidden when browsing.

Resume mode uses the existing task's project scope; the --workspace flag is
ignored in that case. The refiner picks up exactly where the DB left off
(current spec + last-N iteration triples) but with fresh agent-loop state.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import uuid

from rumil.database import DB
from rumil.orchestrators.generative import GenerativeOrchestrator
from rumil.settings import get_settings


async def run(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    settings = get_settings()
    if args.smoke_test:
        settings.rumil_smoke_test = "1"

    db = await DB.create(
        run_id=str(uuid.uuid4()),
        prod=args.prod,
        staged=not args.no_stage,
    )

    if args.resume:
        # Project is inherited from the existing task inside resume().
        run_name = f"resume {args.resume[:8]}"
    else:
        project = await db.get_or_create_project(args.workspace)
        db.project_id = project.id
        run_name = args.headline or args.request[:120]

    await db.init_budget(args.budget)
    await db.create_run(
        name=run_name,
        question_id=None,
        config=settings.capture_config(),
    )

    frontend = settings.frontend_url
    print(f"Trace: {frontend}/traces/{db.run_id}\n")
    if args.resume:
        print(f"Resuming task: {args.resume}")
    else:
        print(f"Workspace: {args.workspace}  (project_id={db.project_id})")
    print(f"Budget:    {args.budget}\n")

    orchestrator = GenerativeOrchestrator(
        db,
        refine_max_rounds=args.refine_max_rounds,
    )
    if args.resume:
        result = await orchestrator.resume(args.resume)
    else:
        result = await orchestrator.run(args.request, headline=args.headline)

    print("\n--- Orchestrator result ---")
    print(f"Task:       {result.task_id[:8]}  ({result.task_id})")
    print(
        f"Artefact:   "
        f"{result.artefact_id[:8] if result.artefact_id else '—'}  "
        f"({result.artefact_id or 'no artefact produced'})"
    )
    print(f"Finalized:  {result.finalized}")

    if not result.artefact_id:
        print("\nNo artefact produced. Check the trace for budget/error details.")
        return 1

    artefact = await db.get_page(result.artefact_id)
    if artefact is None:
        print("\nArtefact page lookup failed after orchestrator return.")
        return 1

    print(f"\n--- Artefact: {artefact.headline} ---\n")
    print(artefact.content)
    print()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the generative workflow (spec -> artefact -> critique -> refine) end-to-end."
    )
    parser.add_argument(
        "request",
        nargs="?",
        default=None,
        help=(
            "The user request describing the artefact to produce. "
            "Required unless --resume <task-id> is given."
        ),
    )
    parser.add_argument(
        "--resume",
        default=None,
        metavar="TASK_ID",
        help=(
            "Resume an existing artefact task (e.g. after interrupting a prior run). "
            "Loads the task + current spec + last-N triples and re-runs refine_spec. "
            "When set, the positional request argument and --workspace are ignored."
        ),
    )
    parser.add_argument(
        "--headline",
        default=None,
        help="Short label for the artefact task (defaults to first 120 chars of request).",
    )
    parser.add_argument(
        "--workspace",
        default="generative-scratch",
        help="Project name to scope the run into (default: generative-scratch).",
    )
    parser.add_argument(
        "--budget",
        type=int,
        default=30,
        help="Total budget for the run. generate_spec ~1, refine_spec ~1, "
        "each regenerate_and_critique ~3 (one artefact + workspace-aware critique "
        "+ request-only critique). 30 covers ~9 regenerations.",
    )
    parser.add_argument(
        "--refine-max-rounds",
        type=int,
        default=10,
        help="Max agent-loop rounds for the refiner (default: 10).",
    )
    parser.add_argument(
        "--prod",
        action="store_true",
        help="Target the production Supabase instance instead of local.",
    )
    parser.add_argument(
        "--no-stage",
        action="store_true",
        help="Disable staged-run isolation (default is staged).",
    )
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="Set rumil_smoke_test=1 (caps agent loops at 2 rounds, uses Haiku).",
    )
    args = parser.parse_args()

    if args.resume and args.request:
        parser.error("--resume cannot be combined with a positional request.")
    if not args.resume and not args.request:
        parser.error("either a positional request or --resume <task-id> is required.")

    try:
        exit_code = asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        exit_code = 130
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
