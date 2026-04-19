"""Run a single call end-to-end against the local database.

Usage:
    # Find considerations on a question (creates one if needed)
    uv run python scripts/run_call.py find-considerations "Is the sky blue?"

    # Find considerations on an existing question by ID
    uv run python scripts/run_call.py find-considerations --question-id <UUID>

    # Assess an existing question
    uv run python scripts/run_call.py assess --question-id <UUID>

    # Override find-considerations params
    uv run python scripts/run_call.py find-considerations "Why is water wet?" --mode concrete --max-rounds 3

    # Use smoke-test model (haiku)
    uv run python scripts/run_call.py find-considerations "Test question" --smoke-test

    # Use a custom workspace
    uv run python scripts/run_call.py find-considerations "Test question" --workspace my-scratch

    # Run only up to a specific stage (build_context or update_workspace)
    uv run python scripts/run_call.py find-considerations "Test question" --up-to-stage build_context
    uv run python scripts/run_call.py find-considerations "Test question" --up-to-stage update_workspace

All runs are staged by default (use --no-stage to disable). Workspace defaults to
'default' but is auto-detected from --question-id when possible.
"""

import argparse
import asyncio
import logging
import uuid

from rumil.calls.build_model import BuildModelCall
from rumil.calls.call_registry import ASSESS_CALL_CLASSES, get_call_runner_class
from rumil.calls.find_considerations import FindConsiderationsCall
from rumil.calls.link_subquestions import LinkSubquestionsCall
from rumil.calls.stages import CallRunner
from rumil.calls.web_research import WebResearchCall
from rumil.database import DB
from rumil.models import CallStage, CallType, FindConsiderationsMode, ModelFlavor
from rumil.orchestrators import create_root_question
from rumil.orchestrators.robustify import RobustifyOrchestrator
from rumil.settings import get_settings

_SCOUT_CALL_TYPES_ORDERED: tuple[CallType, ...] = (
    CallType.SCOUT_SUBQUESTIONS,
    CallType.SCOUT_ESTIMATES,
    CallType.SCOUT_HYPOTHESES,
    CallType.SCOUT_ANALOGIES,
    CallType.SCOUT_PARADIGM_CASES,
    CallType.SCOUT_FACTCHECKS,
    CallType.SCOUT_WEB_QUESTIONS,
    CallType.SCOUT_DEEP_QUESTIONS,
)

_SCOUT_CALL_TYPES: dict[str, tuple[CallType, type[CallRunner]]] = {
    ct.value.replace("_", "-"): (ct, get_call_runner_class(ct)) for ct in _SCOUT_CALL_TYPES_ORDERED
}


async def run_call(args: argparse.Namespace, db: DB, question_id: str) -> None:
    """Execute a single call (find-considerations/assess/etc.) against the given DB."""
    settings = get_settings()

    call_type = args.call_type
    up_to_stage = CallStage(args.up_to_stage) if args.up_to_stage else None

    if call_type == "find-considerations":
        mode = FindConsiderationsMode(args.mode)
        call = await db.create_call(
            CallType.FIND_CONSIDERATIONS,
            scope_page_id=question_id,
        )
        scout = FindConsiderationsCall(
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
        extra_kwargs: dict = {}
        guidance = getattr(args, "guidance", None)
        if guidance:
            extra_kwargs["guidance"] = guidance
        assess = cls(question_id, call, db, up_to_stage=up_to_stage, **extra_kwargs)
        await assess.run()

    elif call_type == "web-research":
        call = await db.create_call(
            CallType.WEB_RESEARCH,
            scope_page_id=question_id,
        )
        web_research = WebResearchCall(
            question_id,
            call,
            db,
            up_to_stage=up_to_stage,
        )
        await web_research.run()

    elif call_type == "build-model":
        call = await db.create_call(
            CallType.BUILD_MODEL,
            scope_page_id=question_id,
        )
        build_model = BuildModelCall(
            question_id,
            call,
            db,
            flavor=ModelFlavor(args.flavor),
            up_to_stage=up_to_stage,
        )
        await build_model.run()

    elif call_type in _SCOUT_CALL_TYPES:
        scout_ct, cls = _SCOUT_CALL_TYPES[call_type]
        call = await db.create_call(scout_ct, scope_page_id=question_id)
        instance = cls(
            question_id,
            call,
            db,
            max_rounds=args.max_rounds,
            fruit_threshold=args.fruit_threshold,
            up_to_stage=up_to_stage,
        )
        await instance.run()

    elif call_type == "link-subquestions":
        call = await db.create_call(
            CallType.LINK_SUBQUESTIONS,
            scope_page_id=question_id,
        )
        linker = LinkSubquestionsCall(
            question_id,
            call,
            db,
            up_to_stage=up_to_stage,
        )
        await linker.run()

    elif call_type == "robustify":
        if up_to_stage:
            print("--up-to-stage is not supported for robustify.")
            return
        orch = RobustifyOrchestrator(db, max_rounds=args.max_rounds)
        variant_ids = await orch.run(question_id)
        print(f"\nProduced {len(variant_ids)} variant(s):")
        for vid in variant_ids:
            page = await db.get_page(vid)
            if page:
                print(f"  {vid[:8]} credence={page.credence} — {page.headline}")

    else:
        print(f"Unknown call type: {call_type}")


async def _apply_pin_prompt_flags(flags: list[str], db: DB) -> None:
    """Resolve NAME=HASH pairs against prompt_versions and install overrides."""
    from rumil.llm import pin_prompt_content

    for raw in flags:
        if "=" not in raw:
            raise SystemExit(f"--pin-prompt must be NAME=HASH; got {raw!r}")
        name, hash_prefix = raw.split("=", 1)
        name = name.strip()
        hash_prefix = hash_prefix.strip()
        if not name or not hash_prefix:
            raise SystemExit(f"--pin-prompt must be NAME=HASH; got {raw!r}")
        q = (
            db.client.table("prompt_versions")
            .select("hash,content")
            .eq("name", name)
            .like("hash", f"{hash_prefix}%")
            .limit(2)
        )
        rows = list((await db._execute(q)).data or [])
        if not rows:
            raise SystemExit(
                f"--pin-prompt: no prompt_versions row matches name={name!r} hash prefix={hash_prefix!r}"
            )
        if len(rows) > 1:
            raise SystemExit(
                f"--pin-prompt: ambiguous hash prefix {hash_prefix!r} for name={name!r} "
                f"(matches at least {len(rows)} rows — disambiguate)"
            )
        pin_prompt_content(name, rows[0]["content"])
        print(f"Pinned prompt name={name} hash={rows[0]['hash'][:12]}")


async def run(args: argparse.Namespace) -> None:
    settings = get_settings()
    if args.available_moves is not None:
        settings.available_moves = args.available_moves
    if args.available_calls is not None:
        settings.available_calls = args.available_calls
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
    staged = not args.no_stage
    db = await DB.create(run_id=str(uuid.uuid4()), prod=args.prod, staged=staged)
    project, _ = await db.get_or_create_project(workspace)
    db.project_id = project.id

    frontend = settings.frontend_url

    if args.question_id:
        question_id = args.question_id
        page = await db.get_page(question_id)
        if not page:
            print(f"Question {question_id} not found.")
            return
        if page.project_id and page.project_id != db.project_id:
            db.project_id = page.project_id
        question_text = page.headline
        print(f"Using existing question: {question_text}")
    elif args.question_text:
        question_text = args.question_text
        question_id = await create_root_question(question_text, db)
        print(f"Created question: {question_id[:8]}")
    else:
        print("Provide a question text or --question-id.")
        return

    if args.pin_prompt:
        await _apply_pin_prompt_flags(args.pin_prompt, db)

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


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a single call end-to-end.")
    parser.add_argument(
        "call_type",
        choices=[
            "find-considerations",
            "assess",
            "robustify",
            "web-research",
            "build-model",
            "link-subquestions",
            *_SCOUT_CALL_TYPES,
        ],
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
        "--name",
        default="",
        help="Optional name for the run (defaults to question text)",
    )
    parser.add_argument(
        "--workspace",
        default="default",
        help="Project workspace name (default: 'default'). "
        "Auto-detected from --question-id when possible.",
    )
    parser.add_argument(
        "--available-moves",
        dest="available_moves",
        default=None,
        help="Available-moves preset name (default: 'default'). Controls which moves are available per call type.",
    )
    parser.add_argument(
        "--available-calls",
        dest="available_calls",
        default=None,
        help="Available-calls preset name (default: 'default'). Controls which scout/dispatch types the two-phase orchestrator uses.",
    )
    parser.add_argument(
        "--prod",
        action="store_true",
        help="Use the production database instead of local Supabase",
    )
    parser.add_argument(
        "--no-stage",
        action="store_true",
        dest="no_stage",
        help="Run without staging (default: runs are staged)",
    )
    parser.add_argument(
        "--guidance",
        default="",
        help="Optional guidance text appended to the assess task prompt (big assess only)",
    )
    parser.add_argument(
        "--flavor",
        default="theoretical",
        choices=[f.value for f in ModelFlavor],
        help="build-model flavor (default: theoretical). Only theoretical is implemented.",
    )
    parser.add_argument(
        "--up-to-stage",
        choices=[s.value for s in CallStage if s != CallStage.CLOSING_REVIEW],
        default=None,
        help="Stop after this stage (default: run all stages)",
    )
    parser.add_argument(
        "--pin-prompt",
        action="append",
        default=[],
        metavar="NAME=HASH",
        help=(
            "Load prompt content for NAME from prompt_versions.content at the "
            "given sha256 HASH instead of prompts/NAME.md. Use to replay an "
            "experiment with a historical prompt version. Repeatable. "
            "HASH may be a unique short prefix."
        ),
    )
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
