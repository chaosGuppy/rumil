"""Run a single call end-to-end against the local database.

Usage:
    # Scout a question (creates one if needed)
    uv run python scripts/run_call.py scout "Is the sky blue?"

    # Scout an existing question by ID
    uv run python scripts/run_call.py scout --question-id <UUID>

    # Assess an existing question
    uv run python scripts/run_call.py assess --question-id <UUID>

    # Prioritization on an existing question
    uv run python scripts/run_call.py prioritize --question-id <UUID> --budget 5

    # Override scout params
    uv run python scripts/run_call.py scout "Why is water wet?" --mode concrete --max-rounds 3

    # Use smoke-test model (haiku)
    uv run python scripts/run_call.py scout "Test question" --smoke-test

All runs write to the local database under the workspace 'test-calls'.
"""

import argparse
import asyncio
import logging
import uuid

from rumil.calls import run_scout_session
from rumil.calls.assess import run_assess
from rumil.calls.prioritization import run_prioritization
from rumil.database import DB
from rumil.models import CallType, ScoutMode
from rumil.orchestrator import create_root_question
from rumil.settings import get_settings


async def run(args: argparse.Namespace) -> None:
    settings = get_settings()
    if args.smoke_test:
        settings.differential_smoke_test = "1"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    db = await DB.create(run_id=str(uuid.uuid4()))
    project = await db.get_or_create_project("test-calls")
    db.project_id = project.id

    frontend = settings.frontend_url
    print(f"Trace: {frontend}/traces/{db.run_id}\n")

    await db.init_budget(args.budget)

    if args.question_id:
        question_id = args.question_id
        page = await db.get_page(question_id)
        if not page:
            print(f"Question {question_id} not found.")
            return
        print(f"Using existing question: {page.summary}")
    elif args.question_text:
        question_id = await create_root_question(args.question_text, db)
        print(f"Created question: {question_id[:8]}")
    else:
        print("Provide a question text or --question-id.")
        return

    call_type = args.call_type
    print(f"Running {call_type} on {question_id[:8]}...")

    if call_type == "scout":
        mode = ScoutMode(args.mode)
        call = await db.create_call(
            CallType.SCOUT, scope_page_id=question_id,
        )
        await run_scout_session(
            question_id, call, db,
            max_rounds=args.max_rounds,
            fruit_threshold=args.fruit_threshold,
            mode=mode,
        )

    elif call_type == "assess":
        call = await db.create_call(
            CallType.ASSESS, scope_page_id=question_id,
        )
        await run_assess(question_id, call, db)

    elif call_type == "prioritize":
        call = await db.create_call(
            CallType.PRIORITIZATION,
            scope_page_id=question_id,
            budget_allocated=args.budget,
        )
        await run_prioritization(question_id, call, args.budget, db)

    else:
        print(f"Unknown call type: {call_type}")
        return

    print("\nDone.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a single call end-to-end.")
    parser.add_argument(
        "call_type", choices=["scout", "assess", "prioritize"],
        help="Type of call to run",
    )
    parser.add_argument(
        "question_text", nargs="?", default=None,
        help="Question text (creates a new question)",
    )
    parser.add_argument("--question-id", help="Existing question UUID")
    parser.add_argument("--budget", type=int, default=5, help="Budget (default: 5)")
    parser.add_argument(
        "--mode", default="alternate",
        choices=["alternate", "abstract", "concrete"],
        help="Scout mode (default: alternate)",
    )
    parser.add_argument(
        "--max-rounds", type=int, default=5, help="Max scout rounds (default: 5)",
    )
    parser.add_argument(
        "--fruit-threshold", type=int, default=4,
        help="Fruit threshold for stopping (default: 4)",
    )
    parser.add_argument(
        "--smoke-test", action="store_true",
        help="Use smoke-test settings (haiku model, reduced rounds)",
    )
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
