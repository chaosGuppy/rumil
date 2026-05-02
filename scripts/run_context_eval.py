"""Compare what two context builders pull into the prompt for a question.

Workflow:

1. Resolve the question and project (auto-detected from --question-id).
2. Look up an existing gold-standard context build for this question. The
   gold builder is ImpactFilteredContext, which is expensive — so the
   workflow caches it in the runs table (config.eval.role = 'gold') and
   reuses it across invocations unless --refresh-gold-standard is passed.
3. If no gold exists (or --refresh-gold-standard), run ImpactFilteredContext
   against the question with up_to_stage=build_context. The standard
   context_built trace event captures every loaded page.
4. Run the candidate builder (default: EmbeddingContext) the same way.
5. Print a comparison URL pointing at the diff page in the frontend.

Both arms record their own runs row in the runs table. Each arm tags
runs.config.eval with role/context_builder/paired_run_id so the API can
look them up.

Usage:

    uv run python scripts/run_context_eval.py <QUESTION_ID> [flags]

    # Use a different candidate builder
    uv run python scripts/run_context_eval.py <QUESTION_ID> --builder ImpactFilteredContext

    # Force a fresh gold (ignore the cache)
    uv run python scripts/run_context_eval.py <QUESTION_ID> --refresh-gold-standard

    # Pin a specific workspace (otherwise auto-detected from the question)
    uv run python scripts/run_context_eval.py <QUESTION_ID> --workspace my-scratch
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import uuid
from dataclasses import dataclass

from rumil.calls.context_builder_eval import (
    EVAL_CONTEXT_BUILDERS,
    GOLD_CONTEXT_BUILDER,
    ContextBuilderEvalCall,
    make_eval_context_builder,
)
from rumil.database import DB, _rows
from rumil.models import CallType
from rumil.settings import get_settings


@dataclass
class _Resolved:
    question_id: str
    project_id: str


async def _resolve_question_project(
    question_id: str,
    workspace_override: str,
    prod: bool,
) -> _Resolved | None:
    """Find which project a question belongs to.

    If --workspace is left at the default, auto-detect from the question's
    project_id. If --workspace is set explicitly, use that name (verifying
    the question is reachable from that project).
    """
    probe = await DB.create(run_id=str(uuid.uuid4()), prod=prod, staged=False)
    try:
        page = await probe.get_page(question_id)
        if page is None:
            return None

        if workspace_override != "default":
            project = await probe.get_or_create_project(workspace_override)
            return _Resolved(question_id=question_id, project_id=project.id)

        if page.project_id:
            return _Resolved(question_id=question_id, project_id=page.project_id)

        project = await probe.get_or_create_project("default")
        return _Resolved(question_id=question_id, project_id=project.id)
    finally:
        await probe.close()


async def _run_arm(
    *,
    question_id: str,
    project_id: str,
    prod: bool,
    builder_name: str,
    role: str,
    paired_run_id: str | None,
) -> str:
    """Create a run + call and execute the named builder; return run_id.

    Tags the run with config.eval ONLY after the call completes — a
    failed build_context leaves the run untagged, so find_eval_gold_run
    won't surface it as a usable cache hit on subsequent invocations.
    """
    run_id = str(uuid.uuid4())
    db = await DB.create(run_id=run_id, prod=prod, staged=False)
    db.project_id = project_id
    try:
        settings = get_settings()
        base_config = settings.capture_config()
        name = f"context-eval {role}: {builder_name}"
        await db.create_run(name=name, question_id=question_id, config=base_config)
        await db.init_budget(0)

        call = await db.create_call(
            CallType.CONTEXT_BUILDER_EVAL,
            scope_page_id=question_id,
        )
        builder = make_eval_context_builder(builder_name, CallType.CONTEXT_BUILDER_EVAL)
        runner = ContextBuilderEvalCall(
            question_id,
            call,
            db,
            builder=builder,
            builder_name=builder_name,
        )
        await runner.run()
        await db.set_run_eval_meta(
            run_id,
            role=role,
            context_builder=builder_name,
            question_id=question_id,
            paired_run_id=paired_run_id,
        )
    finally:
        await db.close()
    return run_id


async def _find_existing_gold(
    *,
    question_id: str,
    project_id: str,
    prod: bool,
) -> str | None:
    db = await DB.create(run_id=str(uuid.uuid4()), prod=prod, staged=False)
    try:
        db.project_id = project_id
        return await db.find_eval_gold_run(question_id, GOLD_CONTEXT_BUILDER)
    finally:
        await db.close()


async def _patch_gold_partner(
    *,
    project_id: str,
    prod: bool,
    gold_run_id: str,
    candidate_run_id: str,
) -> None:
    db = await DB.create(run_id=str(uuid.uuid4()), prod=prod, staged=False)
    try:
        db.project_id = project_id
        await db.update_run_config_eval_partner(gold_run_id, candidate_run_id)
    finally:
        await db.close()


async def _project_name(project_id: str, prod: bool) -> str:
    db = await DB.create(run_id=str(uuid.uuid4()), prod=prod, staged=False)
    try:
        rows = _rows(
            await db._execute(db.client.table("projects").select("name").eq("id", project_id))
        )
        if rows:
            return str(rows[0].get("name") or "")
        return ""
    finally:
        await db.close()


async def run(args: argparse.Namespace) -> None:
    settings = get_settings()
    if args.smoke_test:
        settings.rumil_smoke_test = "1"

    if args.builder not in EVAL_CONTEXT_BUILDERS:
        valid = ", ".join(sorted(EVAL_CONTEXT_BUILDERS))
        print(f"Unknown builder {args.builder!r}. Valid: {valid}")
        return

    question_id: str = args.question_id

    resolved = await _resolve_question_project(question_id, args.workspace, args.prod)
    if resolved is None:
        print(f"Question {question_id} not found.")
        return
    project_id = resolved.project_id
    project_name = await _project_name(project_id, args.prod)
    print(f"Project: {project_name or project_id} ({project_id[:8]})")

    gold_run_id: str | None = None
    if not args.refresh_gold_standard:
        gold_run_id = await _find_existing_gold(
            question_id=question_id,
            project_id=project_id,
            prod=args.prod,
        )

    frontend_url = settings.frontend_url

    if gold_run_id is None:
        if args.refresh_gold_standard:
            print(f"--refresh-gold-standard: rebuilding gold with {GOLD_CONTEXT_BUILDER}...")
        else:
            print(f"No gold run cached; building one with {GOLD_CONTEXT_BUILDER}...")
        gold_run_id = await _run_arm(
            question_id=question_id,
            project_id=project_id,
            prod=args.prod,
            builder_name=GOLD_CONTEXT_BUILDER,
            role="gold",
            paired_run_id=None,
        )
        print(f"  Gold trace: {frontend_url}/traces/{gold_run_id}")
    else:
        print(f"Reusing cached gold run {gold_run_id[:8]}.")

    print(f"Running candidate builder {args.builder}...")
    candidate_run_id = await _run_arm(
        question_id=question_id,
        project_id=project_id,
        prod=args.prod,
        builder_name=args.builder,
        role="candidate",
        paired_run_id=gold_run_id,
    )
    print(f"  Candidate trace: {frontend_url}/traces/{candidate_run_id}")

    await _patch_gold_partner(
        project_id=project_id,
        prod=args.prod,
        gold_run_id=gold_run_id,
        candidate_run_id=candidate_run_id,
    )

    print()
    print(f"Compare: {frontend_url}/context-evals/{gold_run_id}/vs/{candidate_run_id}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare two context builders on the same question.",
    )
    parser.add_argument(
        "question_id",
        help="UUID of the question to evaluate context builders against.",
    )
    parser.add_argument(
        "--builder",
        default="EmbeddingContext",
        choices=sorted(EVAL_CONTEXT_BUILDERS.keys()),
        help="Candidate context builder to compare against the gold (default: EmbeddingContext).",
    )
    parser.add_argument(
        "--refresh-gold-standard",
        action="store_true",
        dest="refresh_gold_standard",
        help="Force a fresh ImpactFilteredContext gold run, ignoring any cached one.",
    )
    parser.add_argument(
        "--workspace",
        default="default",
        help="Project workspace name (default: auto-detect from question).",
    )
    parser.add_argument(
        "--prod",
        action="store_true",
        help="Use the production database.",
    )
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="Use smoke-test settings (haiku model, ImpactFilteredContext bypass).",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
