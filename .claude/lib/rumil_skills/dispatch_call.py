"""Fire one rumil call as Claude Code.

This is the *rumil-mediated* lane: a normal rumil call (find_considerations,
assess, scout, web_research, prioritize, create_view) with all the usual
context-building, prompts, and tools. Claude Code is just the trigger. The
run is tagged with origin=claude-code in runs.config and calls.call_params
so it's distinguishable from a main.py-initiated run.

Usage:
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.dispatch_call \\
        <call_type> <question_id> [--budget N] [--smoke-test]

Call types:
    find-considerations  assess  web-research  create-view
    scout-subquestions  scout-estimates  scout-hypotheses  scout-analogies
    scout-paradigm-cases  scout-factchecks  scout-web-questions
    scout-deep-questions
    scout-c-how-true  scout-c-how-false  scout-c-cruxes
    scout-c-relevant-evidence  scout-c-stress-test-cases
    scout-c-robustify  scout-c-strengthen
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from rumil.calls.call_registry import CALL_RUNNER_CLASSES
from rumil.constants import DEFAULT_FRUIT_THRESHOLD
from rumil.database import DB
from rumil.dispatch import dispatch_single_call
from rumil.models import Call, CallType, FindConsiderationsMode

from ._format import print_event, print_trace, truncate
from ._runctx import make_db, open_run

# Map skill-facing CLI name (dashes) → CallType enum. Every entry with a
# registered runner in CALL_RUNNER_CLASSES is exposed.
_CLI_NAME_TO_CALL_TYPE: dict[str, CallType] = {
    ct.value.replace("_", "-"): ct for ct in CALL_RUNNER_CLASSES
}

CALL_TYPES = sorted(_CLI_NAME_TO_CALL_TYPE.keys())

DEFAULT_BUDGET = 3


async def _dispatch(
    db: DB,
    call_type_str: str,
    question_id: str,
    *,
    budget: int,
    max_rounds: int | None,
) -> Call:
    """Create + run the appropriate CallRunner via dispatch_single_call."""
    call_type = _CLI_NAME_TO_CALL_TYPE.get(call_type_str)
    if call_type is None:
        raise ValueError(f"unknown call type: {call_type_str}")

    # FindConsiderations needs fruit_threshold + a specific mode; other
    # runners accept max_rounds or nothing — dispatch_single_call filters
    # against each runner's signature.
    extra: dict[str, object] = {}
    if call_type == CallType.FIND_CONSIDERATIONS:
        extra["fruit_threshold"] = DEFAULT_FRUIT_THRESHOLD
        extra["mode"] = FindConsiderationsMode.ALTERNATE

    return await dispatch_single_call(
        call_type,
        question_id,
        db,
        max_rounds=max_rounds or 5,
        origin="rumil-dispatch",
        extra_runner_kwargs=extra,
    )


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("call_type", choices=CALL_TYPES)
    parser.add_argument("question_id", help="Full or short (8-char) question ID")
    parser.add_argument("--workspace", default=None)
    parser.add_argument("--budget", type=int, default=DEFAULT_BUDGET)
    parser.add_argument("--max-rounds", type=int, default=None)
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="Faster/cheaper model, fewer rounds (for testing)",
    )
    args = parser.parse_args()

    if args.smoke_test:
        get_settings().rumil_smoke_test = "1"

    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s %(name)s %(message)s",
    )

    db, ws = await make_db(workspace=args.workspace)
    try:
        full_id = await db.resolve_page_id(args.question_id)
        if not full_id:
            print(f"no question matching {args.question_id!r} in workspace {ws!r}")
            sys.exit(1)
        page = await db.get_page(full_id)
        if page is None:
            print(f"page {full_id[:8]} vanished mid-lookup")
            sys.exit(1)
        if page.project_id and page.project_id != db.project_id:
            db.project_id = page.project_id

        print(f"workspace: {ws}")
        print(f"question:  {full_id[:8]}  {truncate(page.headline, 80)}")

        await open_run(
            db,
            name=page.headline,
            question_id=full_id,
            skill="rumil-dispatch",
            budget=args.budget,
            extra_config={
                "call_type": args.call_type,
                "smoke_test": bool(args.smoke_test),
            },
        )
        print_trace(db.run_id)

        print_event("→", f"firing {args.call_type} (budget {args.budget})")
        call = await _dispatch(
            db,
            args.call_type,
            full_id,
            budget=args.budget,
            max_rounds=args.max_rounds,
        )
        total, used = await db.get_budget()
        # Refresh the call to pick up any review/cost updates.
        refreshed = await db.get_call(call.id) or call
        cost_s = f"${refreshed.cost_usd:.3f}" if refreshed.cost_usd is not None else "—"
        print_event(
            "✓",
            f"done: status={refreshed.status.value} cost={cost_s} budget={used}/{total}",
        )
        if refreshed.result_summary:
            print()
            print(refreshed.result_summary.rstrip())
    finally:
        await db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
