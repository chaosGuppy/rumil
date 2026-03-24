"""Run a single call end-to-end against the local database.

Usage:
    # Find considerations on a question (creates one if needed)
    uv run python scripts/run_call.py find-considerations "Is the sky blue?"

    # Find considerations on an existing question by ID
    uv run python scripts/run_call.py find-considerations --question-id <UUID>

    # Assess an existing question
    uv run python scripts/run_call.py assess --question-id <UUID>

    # Prioritization on an existing question
    uv run python scripts/run_call.py prioritize --question-id <UUID> --budget 5

    # Override find-considerations params
    uv run python scripts/run_call.py find-considerations "Why is water wet?" --mode concrete --max-rounds 3

    # Use smoke-test model (haiku)
    uv run python scripts/run_call.py find-considerations "Test question" --smoke-test

    # Use a custom workspace
    uv run python scripts/run_call.py find-considerations "Test question" --workspace my-scratch

    # A/B test a call (requires .a.env and .b.env)
    uv run python scripts/run_call.py find-considerations "Test question" --ab --smoke-test

    # Run only up to a specific stage (build_context or create_pages)
    uv run python scripts/run_call.py find-considerations "Test question" --up-to-stage build_context
    uv run python scripts/run_call.py find-considerations "Test question" --up-to-stage create_pages

All runs write to the local database under the workspace 'test-calls' by default.
"""

import argparse
import asyncio
import logging
import uuid

from rumil.calls.call_registry import (
    ASSESS_CALL_CLASSES,
    FIND_CONSIDERATIONS_CALL_CLASSES,
    WEB_RESEARCH_CALL_CLASSES,
)
from rumil.calls.prioritization import run_prioritization
from rumil.database import DB
from rumil.models import CallStage, CallType, FindConsiderationsMode
from rumil.orchestrator import create_root_question
from rumil.settings import Settings, get_settings, _settings_var


async def run_call(args: argparse.Namespace, db: DB, question_id: str) -> None:
    """Execute a single call (find-considerations/assess/prioritize) against the given DB."""
    settings = get_settings()

    call_type = args.call_type
    up_to_stage = CallStage(args.up_to_stage) if args.up_to_stage else None

    if call_type == "find-considerations":
        mode = FindConsiderationsMode(args.mode)
        call = await db.create_call(
            CallType.FIND_CONSIDERATIONS,
            scope_page_id=question_id,
        )
        cls = FIND_CONSIDERATIONS_CALL_CLASSES[
            settings.find_considerations_call_variant
        ]
        scout = cls(
            question_id,
            call,
            db,
            max_rounds=args.max_rounds,
            fruit_threshold=args.fruit_threshold,
            mode=mode,
            up_to_stage=up_to_stage,
        )
        await scout.run()

    elif call_type == "assess":
        call = await db.create_call(
            CallType.ASSESS,
            scope_page_id=question_id,
        )
        cls = ASSESS_CALL_CLASSES[settings.assess_call_variant]
        assess = cls(question_id, call, db, up_to_stage=up_to_stage)
        await assess.run()

    elif call_type == "web-research":
        call = await db.create_call(
            CallType.WEB_RESEARCH,
            scope_page_id=question_id,
        )
        cls = WEB_RESEARCH_CALL_CLASSES[settings.web_research_call_variant]
        web_research = cls(
            question_id,
            call,
            db,
            up_to_stage=up_to_stage,
        )
        await web_research.run()

    elif call_type == "prioritize":
        if up_to_stage:
            print("--up-to-stage is not supported for prioritize calls.")
            return
        call = await db.create_call(
            CallType.PRIORITIZATION,
            scope_page_id=question_id,
            budget_allocated=args.budget,
        )
        await run_prioritization(question_id, call, args.budget, db)

    else:
        print(f"Unknown call type: {call_type}")


async def run(args: argparse.Namespace) -> None:
    settings = get_settings()
    if args.moves_preset is not None:
        settings.move_preset = args.moves_preset
    if args.smoke_test:
        settings.rumil_smoke_test = "1"
    if args.force_twophase_recurse:
        settings.force_twophase_recurse = True

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    workspace = args.workspace
    db = await DB.create(run_id=str(uuid.uuid4()))
    project = await db.get_or_create_project(workspace)
    db.project_id = project.id

    frontend = settings.frontend_url

    if args.question_id:
        question_id = args.question_id
        page = await db.get_page(question_id)
        if not page:
            print(f"Question {question_id} not found.")
            return
        question_text = page.headline
        print(f"Using existing question: {question_text}")
    elif args.question_text:
        question_text = args.question_text
        question_id = await create_root_question(question_text, db)
        print(f"Created question: {question_id[:8]}")
    else:
        print("Provide a question text or --question-id.")
        return

    if args.ab:
        await _run_ab(args, db, question_id, question_text, frontend)
        return

    print(f"Trace: {frontend}/traces/{db.run_id}\n")
    await db.init_budget(args.budget)

    name = args.name or question_text
    config = settings.capture_config()
    await db.create_run(
        name=name,
        question_id=question_id,
        config=config,
    )

    print(f"Running {args.call_type} on {question_id[:8]}...")
    await run_call(args, db, question_id)
    print("\nDone.")


async def _run_ab(
    args: argparse.Namespace,
    db: DB,
    question_id: str,
    question_text: str,
    frontend: str,
) -> None:
    """Run an A/B test: two concurrent calls with different configs."""
    ab_run_id = str(uuid.uuid4())
    name = args.name or question_text

    await db.create_ab_run(ab_run_id, name, question_id)

    print(f"\nAB test: {ab_run_id}")
    print(f"Question: {question_text}")
    print(f"Budget per arm: {args.budget}")
    print(f"Trace: {frontend}/ab-traces/{ab_run_id}")

    parent_settings = get_settings()

    async def run_arm(arm_label: str, env_file: str) -> None:
        arm_settings = Settings.from_env_files(".env", env_file)
        if parent_settings.is_smoke_test:
            arm_settings.rumil_smoke_test = "1"
        _settings_var.set(arm_settings)

        arm_db = await DB.create(
            run_id=str(uuid.uuid4()),
            client=db.client,
            project_id=db.project_id,
            ab_run_id=ab_run_id,
        )
        config = arm_settings.capture_config()
        await arm_db.create_run(
            name=f"{name} (arm {arm_label})",
            question_id=question_id,
            config=config,
            ab_arm=arm_label,
        )
        await arm_db.init_budget(args.budget)

        print(f"\nRunning {args.call_type} arm {arm_label}...")
        await run_call(args, db=arm_db, question_id=question_id)
        total, used = await arm_db.get_budget()
        print(f"\nArm {arm_label} complete: {used}/{total} budget used")

    async with asyncio.TaskGroup() as tg:
        tg.create_task(run_arm("a", ".a.env"))
        tg.create_task(run_arm("b", ".b.env"))

    print(f"\nAB test complete: {frontend}/ab-traces/{ab_run_id}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a single call end-to-end.")
    parser.add_argument(
        "call_type",
        choices=["find-considerations", "assess", "prioritize", "web-research"],
        help="Type of call to run",
    )
    parser.add_argument(
        "question_text",
        nargs="?",
        default=None,
        help="Question text (creates a new question)",
    )
    parser.add_argument("--question-id", help="Existing question UUID")
    parser.add_argument("--budget", type=int, default=5, help="Budget (default: 5)")
    parser.add_argument(
        "--mode",
        default="alternate",
        choices=["alternate", "abstract", "concrete"],
        help="Find-considerations mode (default: alternate)",
    )
    parser.add_argument(
        "--max-rounds",
        type=int,
        default=5,
        help="Max rounds (default: 5)",
    )
    parser.add_argument(
        "--fruit-threshold",
        type=int,
        default=4,
        help="Fruit threshold for stopping (default: 4)",
    )
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="Use smoke-test settings (haiku model, reduced rounds)",
    )
    parser.add_argument(
        "--force-twophase-recurse",
        action="store_true",
        dest="force_twophase_recurse",
        help="Force the two-phase orchestrator to dispatch two recurse calls",
    )
    parser.add_argument(
        "--ab",
        action="store_true",
        help="Run the call as an A/B test (requires .a.env and .b.env)",
    )
    parser.add_argument(
        "--name",
        default="",
        help="Optional name for the run (defaults to question text)",
    )
    parser.add_argument(
        "--workspace",
        default="test-calls",
        help="Project workspace name (default: test-calls)",
    )
    parser.add_argument(
        "--moves-preset",
        dest="moves_preset",
        default=None,
        help="Move preset name (default: 'default'). Controls which moves are available per call type.",
    )
    parser.add_argument(
        "--up-to-stage",
        choices=[s.value for s in CallStage if s != CallStage.CLOSING_REVIEW],
        default=None,
        help="Stop after this stage (default: run all stages)",
    )
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
