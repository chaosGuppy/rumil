"""Run TwoPhaseOrchestrator._get_next_batch on a question and print the result.

Usage:
    uv run python scripts/run_prio.py --question-id <UUID> --budget 10

    # Use smoke-test settings
    uv run python scripts/run_prio.py --question-id <UUID> --budget 10 --smoke-test

    # Use a custom workspace
    uv run python scripts/run_prio.py --question-id <UUID> --budget 10 --workspace my-scratch
"""

import argparse
import asyncio
import json
import logging
import uuid

from rumil.database import DB
from rumil.orchestrators import TwoPhaseOrchestrator
from rumil.settings import get_settings


async def run(args: argparse.Namespace) -> None:
    settings = get_settings()
    if args.smoke_test:
        settings.rumil_smoke_test = "1"
    if args.available_calls is not None:
        settings.available_calls = args.available_calls

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    db = await DB.create(run_id=str(uuid.uuid4()))

    page = await db.get_page(args.question_id)
    if not page:
        print(f"Question {args.question_id} not found.")
        return

    if page.project_id:
        db.project_id = page.project_id
    elif args.workspace:
        project = await db.get_or_create_project(args.workspace)
        db.project_id = project.id

    frontend = settings.frontend_url
    print(f"Trace: {frontend}/traces/{db.run_id}\n")

    await db.init_budget(args.budget)
    await db.create_run(
        name=page.headline,
        question_id=args.question_id,
        config=settings.capture_config(),
    )

    orch = TwoPhaseOrchestrator(db)
    await orch._setup()

    try:
        result = await orch._get_next_batch(args.question_id, args.budget)
    finally:
        await orch._teardown()

    print("\n=== PrioritizationResult ===")
    print(f"call_id: {result.call_id}")
    print(f"dispatch_sequences: {len(result.dispatch_sequences)}")
    for i, seq in enumerate(result.dispatch_sequences):
        print(f"\n  Sequence {i}:")
        for j, dispatch in enumerate(seq):
            dump = json.loads(dispatch.model_dump_json())
            print(f"    [{j}] {dump}")

    if result.children:
        print(f"\nchildren: {len(result.children)}")
        for _child_orch, child_qid in result.children:
            print(f"  child question: {child_qid}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run TwoPhaseOrchestrator._get_next_batch and print the result.",
    )
    parser.add_argument("--question-id", required=True, help="Question UUID")
    parser.add_argument("--budget", type=int, required=True, help="Budget for prioritization")
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="Use smoke-test settings",
    )
    parser.add_argument(
        "--workspace",
        default=None,
        help="Project workspace name (inferred from question if omitted)",
    )
    parser.add_argument(
        "--available-calls",
        dest="available_calls",
        default=None,
        help="Available-calls preset name",
    )
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
