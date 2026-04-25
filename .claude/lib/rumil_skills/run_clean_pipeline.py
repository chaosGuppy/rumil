"""Wrap rumil's existing clean pipelines (grounding, feedback_update).

This is the rumil-mediated half of /rumil-clean: it triggers the same
rumil-internal pipelines that `main.py --ground` and `main.py --feedback`
trigger, but with an origin=claude-code tag on the run so it's
distinguishable from a CLI-initiated run.

Prereq: the user has already run `/rumil-dispatch evaluate <qid>` to
produce a completed evaluation call, and that call's ID is the input
here.

Usage:
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.run_clean_pipeline \\
        grounding <eval_call_id>

    PYTHONPATH=.claude/lib uv run python -m rumil_skills.run_clean_pipeline \\
        feedback <eval_call_id>

    # Resume a grounding run from a later stage (see main.py --from-stage)
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.run_clean_pipeline \\
        grounding <eval_call_id> --from-stage 3
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from rumil.clean.grounding import run_grounding_feedback
from rumil.models import CallStatus, CallType

from ._format import print_event, print_trace, truncate
from ._runctx import make_db, open_run


async def _load_eval_call(db, eval_call_id: str):
    resolved = await db.resolve_call_id(eval_call_id)
    if not resolved:
        print(f"no call matching {eval_call_id!r}", file=sys.stderr)
        sys.exit(1)
    call = await db.get_call(resolved)
    if call is None:
        print(f"call {resolved[:8]} not found", file=sys.stderr)
        sys.exit(1)
    if call.call_type != CallType.EVALUATE:
        print(
            f"call {resolved[:8]} is a {call.call_type.value}, not an evaluate call",
            file=sys.stderr,
        )
        sys.exit(1)
    if call.status != CallStatus.COMPLETE:
        print(
            f"evaluate call {resolved[:8]} has status {call.status.value}, must be complete",
            file=sys.stderr,
        )
        sys.exit(1)
    return call


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("pipeline", choices=["grounding", "feedback"])
    parser.add_argument("eval_call_id")
    parser.add_argument("--workspace", default=None)
    parser.add_argument("--from-stage", type=int, default=1)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s %(name)s %(message)s",
    )

    db, ws = await make_db(workspace=args.workspace)
    try:
        call = await _load_eval_call(db, args.eval_call_id)
        evaluation_text = (call.review_json or {}).get("evaluation", "")
        if not evaluation_text:
            print(
                f"evaluate call {call.id[:8]} has no evaluation output",
                file=sys.stderr,
            )
            sys.exit(1)
        if not call.scope_page_id:
            print(
                f"evaluate call {call.id[:8]} has no scope question",
                file=sys.stderr,
            )
            sys.exit(1)
        question = await db.get_page(call.scope_page_id)
        if question is None:
            print(f"scope question {call.scope_page_id[:8]} not found", file=sys.stderr)
            sys.exit(1)
        if question.project_id and question.project_id != db.project_id:
            db.project_id = question.project_id

        print(f"workspace: {ws}")
        print(f"question:  {question.id[:8]}  {truncate(question.headline, 80)}")
        print(f"eval call: {call.id[:8]}")

        await open_run(
            db,
            name=f"{args.pipeline} clean: {question.headline}",
            question_id=call.scope_page_id,
            skill="rumil-clean",
            budget=5,
            extra_config={
                "pipeline": args.pipeline,
                "source_eval_call_id": call.id,
                "from_stage": args.from_stage,
            },
        )
        print_trace(db.run_id)

        if args.pipeline == "grounding":
            print_event("→", f"running grounding pipeline (from_stage={args.from_stage})")
            result = await run_grounding_feedback(
                call.scope_page_id,
                evaluation_text,
                db,
                from_stage=args.from_stage,
            )
        else:  # feedback
            # Lazy import — run_feedback_update has a heavier import tree.
            from rumil.clean.feedback import run_feedback_update

            print_event("→", f"running feedback pipeline (from_stage={args.from_stage})")
            result = await run_feedback_update(
                call.scope_page_id,
                evaluation_text,
                db,
                from_stage=args.from_stage,
            )

        print_event("✓", f"done: pipeline call {result.id[:8]}")
        if result.result_summary:
            print()
            print(result.result_summary.rstrip())
    finally:
        await db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
