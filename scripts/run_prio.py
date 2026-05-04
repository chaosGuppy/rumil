"""Run a single prioritization round (get_dispatches + execute_dispatches).

Usage:
    # Run prio on an existing question by ID
    uv run python scripts/run_prio.py --question-id <UUID> --budget 10

    # Run on several questions in parallel
    uv run python scripts/run_prio.py --question-id <UUID1> <UUID2> --budget 10

    # Mix existing question ids with new question texts; everything runs in parallel
    uv run python scripts/run_prio.py --question-id <UUID1> "A new question?" --budget 10

    # Stop after prioritization — see the chosen dispatches without running them
    uv run python scripts/run_prio.py --question-id <UUID> --budget 10 \\
        --up-to-stage get_dispatches

    # Use smoke-test settings (haiku model)
    uv run python scripts/run_prio.py --question-id <UUID> --budget 10 --smoke-test

    # Use a custom workspace
    uv run python scripts/run_prio.py --question-id <UUID> --budget 10 --workspace my-scratch

A single round is run per target: prioritize once, then (unless ``--up-to-stage
get_dispatches``) execute the chosen sequences and recursive children. For a
full multi-round orchestration loop use ``main.py --continue`` instead.

All runs are staged by default (use --no-stage to disable). Workspace defaults
to 'default' but is auto-detected from --question-id when possible. If a
provided question is staged under another run, that run's run_id is adopted
(budget is added to rather than clobbered; no new ``runs`` row is created).

When multiple targets are passed, each gets its own run_id / DB / staging
adoption logic and the rounds execute concurrently via asyncio.gather.
"""

import argparse
import asyncio
import json
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from rumil.database import DB
from rumil.orchestrators import (
    OrchestrationStage,
    PrioritizationResult,
    TwoPhaseOrchestrator,
    create_root_question,
)
from rumil.settings import get_settings
from rumil.tracing import get_langfuse


def _print_result(question_id: str, result: PrioritizationResult) -> None:
    print(f"\n=== PrioritizationResult for {question_id[:8]} ===")
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


@dataclass
class _TaskPlan:
    """Resolved per-target state, populated by ``_prepare_task`` before execution."""

    db: DB
    question_id: str
    headline: str
    run_id: str
    adopted_run_id: str | None
    is_new_question: bool
    frontend_url: str
    langfuse_url: str | None


async def _prepare_task(
    args: argparse.Namespace,
    *,
    question_id: str | None,
    question_text: str | None,
) -> _TaskPlan | None:
    """Resolve staging/project/question/budget/run-row for one target.

    Runs quietly (no logging.basicConfig yet) so the gathered plan list can be
    printed as a clean header before the noisy execution phase begins.
    """
    settings = get_settings()
    workspace = args.workspace
    staged_flag = not args.no_stage

    adopted_run_id: str | None = None
    if question_id:
        probe = await DB.create(run_id=str(uuid.uuid4()), prod=args.prod, staged=False)
        info = await probe.get_page_staging_info(question_id)
        if info is None:
            print(f"Question {question_id} not found.")
            return None
        is_staged, owning_run_id, _owning_project_id = info
        if is_staged:
            if not staged_flag:
                print(
                    f"Question {question_id[:8]} is staged under run {owning_run_id[:8]}; "
                    "cannot run with --no-stage. Drop --no-stage to adopt the staged run."
                )
                return None
            adopted_run_id = owning_run_id

    run_id = adopted_run_id or str(uuid.uuid4())
    db = await DB.create(run_id=run_id, prod=args.prod, staged=staged_flag)
    project = await db.get_or_create_project(workspace)
    db.project_id = project.id

    is_new_question = False
    if question_id:
        page = await db.get_page(question_id)
        if not page:
            print(f"Question {question_id} not found.")
            return None
        if page.project_id and page.project_id != db.project_id:
            db.project_id = page.project_id
        resolved_text = page.headline
        resolved_qid = question_id
    else:
        assert question_text is not None
        resolved_text = question_text
        resolved_qid = await create_root_question(resolved_text, db)
        is_new_question = True

    if adopted_run_id:
        await db.add_budget(args.budget)
    else:
        await db.init_budget(args.budget)

    name = args.name or resolved_text
    config = settings.capture_config()
    if not adopted_run_id:
        await db.create_run(
            name=name, question_id=resolved_qid, config=config, entrypoint="run_prio"
        )

    langfuse_url: str | None = None
    if get_langfuse() is not None:
        lf_base = settings.langfuse_base_url.rstrip("/")
        langfuse_url = f"{lf_base}/sessions?sessionId={db.run_id}"

    return _TaskPlan(
        db=db,
        question_id=resolved_qid,
        headline=resolved_text,
        run_id=db.run_id,
        adopted_run_id=adopted_run_id,
        is_new_question=is_new_question,
        frontend_url=settings.frontend_url,
        langfuse_url=langfuse_url,
    )


def _print_plan_header(plans: Sequence[_TaskPlan], *, title: str = "Targets") -> None:
    if not plans:
        return
    print()
    print(f"=== {title} ({len(plans)}) ===")
    for p in plans:
        if p.is_new_question:
            kind = "NEW    "
        elif p.adopted_run_id:
            kind = "ADOPTED"
        else:
            kind = "EXIST  "
        headline = p.headline if len(p.headline) <= 100 else p.headline[:97] + "..."
        print(f"  [{kind}] q={p.question_id[:8]} run={p.run_id[:8]}  {headline}")
        print(f"           Trace: {p.frontend_url}/traces/{p.run_id}")
        if p.langfuse_url:
            print(f"           Langfuse: {p.langfuse_url}")
    print()


async def _execute_task(args: argparse.Namespace, plan: _TaskPlan) -> None:
    up_to_stage = OrchestrationStage(args.up_to_stage) if args.up_to_stage else None
    orch = TwoPhaseOrchestrator(plan.db)
    orch.run_initial_scouts_only = args.initial_scouts_only
    await orch._setup()
    try:
        print(f"Prioritizing {plan.question_id[:8]} (budget={args.budget})...")
        result = await orch.get_dispatches(
            plan.question_id,
            args.budget,
            total_remaining=args.budget,
        )
        _print_result(plan.question_id, result)

        if up_to_stage == OrchestrationStage.GET_DISPATCHES:
            print(f"\nStopping after get_dispatches for {plan.question_id[:8]}.")
            return

        if not result.dispatch_sequences and not result.children:
            print(f"\nNothing to execute for {plan.question_id[:8]}.")
            return

        if args.initial_scouts_only:
            print(f"\nExecuting scout dispatches only for {plan.question_id[:8]}...")
        else:
            print(f"\nExecuting dispatches for {plan.question_id[:8]}...")
        await orch.execute_dispatches(result, plan.question_id)
        print(f"Done with {plan.question_id[:8]}.")
    finally:
        await orch._teardown()


async def run(args: argparse.Namespace) -> None:
    settings = get_settings()
    if args.smoke_test:
        settings.rumil_smoke_test = "1"
    if args.available_calls is not None:
        settings.available_calls = args.available_calls
    if args.available_moves is not None:
        settings.available_moves = args.available_moves
    if args.force_twophase_recurse:
        settings.force_twophase_recurse = True

    question_ids: list[str] = list(args.question_id) if args.question_id else []
    question_texts: list[str] = list(args.question_text) if args.question_text else []

    if not question_ids and not question_texts:
        print("Provide at least one question text or --question-id.")
        return

    prep_tasks = [
        _prepare_task(args, question_id=qid, question_text=None) for qid in question_ids
    ] + [_prepare_task(args, question_id=None, question_text=qt) for qt in question_texts]
    plan_results = await asyncio.gather(*prep_tasks)
    plans: list[_TaskPlan] = [p for p in plan_results if p is not None]
    if not plans:
        return

    _print_plan_header(plans)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    await asyncio.gather(*(_execute_task(args, p) for p in plans))
    _print_plan_header(plans, title="Recap")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a single prioritization round (get_dispatches + execute_dispatches).",
    )
    parser.add_argument(
        "question_text",
        nargs="*",
        default=[],
        help="One or more question texts. Each creates a new question; multiple "
        "texts (and/or --question-id values) run in parallel.",
    )
    parser.add_argument(
        "--question-id",
        nargs="+",
        metavar="UUID",
        help="One or more existing question UUIDs. Combined with positional "
        "question texts, all targets run in parallel (each gets its own run_id).",
    )
    parser.add_argument("--budget", type=int, required=True, help="Budget per question")
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
        help="Available-moves preset name (default: 'default').",
    )
    parser.add_argument(
        "--available-calls",
        dest="available_calls",
        default=None,
        help="Available-calls preset name (default: 'default').",
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
        "--up-to-stage",
        choices=[OrchestrationStage.GET_DISPATCHES.value],
        default=None,
        help="Stop after this stage. 'get_dispatches' produces the prioritization "
        "plan and prints it without executing the chosen calls.",
    )
    parser.add_argument(
        "--initial-scouts-only",
        action="store_true",
        dest="initial_scouts_only",
        help="Execute only the scout dispatches (call_type starting with 'scout_') "
        "from the prioritization plan; skip recursive children and other dispatch "
        "types. Useful for assessing the quality of scout outputs (e.g. subquestion "
        "generation) without firing off downstream work.",
    )
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
