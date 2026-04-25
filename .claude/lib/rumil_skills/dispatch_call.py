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

from rumil.calls.call_registry import ASSESS_CALL_CLASSES
from rumil.calls.create_view import CreateViewCall
from rumil.calls.find_considerations import FindConsiderationsCall
from rumil.calls.scout_analogies import ScoutAnalogiesCall
from rumil.calls.scout_c_cruxes import ScoutCCruxesCall
from rumil.calls.scout_c_how_false import ScoutCHowFalseCall
from rumil.calls.scout_c_how_true import ScoutCHowTrueCall
from rumil.calls.scout_c_relevant_evidence import ScoutCRelevantEvidenceCall
from rumil.calls.scout_c_robustify import ScoutCRobustifyCall
from rumil.calls.scout_c_strengthen import ScoutCStrengthenCall
from rumil.calls.scout_c_stress_test_cases import ScoutCStressTestCasesCall
from rumil.calls.scout_deep_questions import ScoutDeepQuestionsCall
from rumil.calls.scout_estimates import ScoutEstimatesCall
from rumil.calls.scout_factchecks import ScoutFactchecksCall
from rumil.calls.scout_hypotheses import ScoutHypothesesCall
from rumil.calls.scout_paradigm_cases import ScoutParadigmCasesCall
from rumil.calls.scout_subquestions import ScoutSubquestionsCall
from rumil.calls.scout_web_questions import ScoutWebQuestionsCall
from rumil.calls.stages import CallRunner
from rumil.calls.web_research import WebResearchCall
from rumil.constants import DEFAULT_FRUIT_THRESHOLD
from rumil.database import DB
from rumil.models import Call, CallType
from rumil.settings import get_settings

from ._format import print_event, print_trace, truncate
from ._runctx import make_db, open_run

_SCOUT_MAP: dict[str, tuple[CallType, type[CallRunner]]] = {
    "scout-subquestions": (CallType.SCOUT_SUBQUESTIONS, ScoutSubquestionsCall),
    "scout-estimates": (CallType.SCOUT_ESTIMATES, ScoutEstimatesCall),
    "scout-hypotheses": (CallType.SCOUT_HYPOTHESES, ScoutHypothesesCall),
    "scout-analogies": (CallType.SCOUT_ANALOGIES, ScoutAnalogiesCall),
    "scout-paradigm-cases": (CallType.SCOUT_PARADIGM_CASES, ScoutParadigmCasesCall),
    "scout-factchecks": (CallType.SCOUT_FACTCHECKS, ScoutFactchecksCall),
    "scout-web-questions": (CallType.SCOUT_WEB_QUESTIONS, ScoutWebQuestionsCall),
    "scout-deep-questions": (CallType.SCOUT_DEEP_QUESTIONS, ScoutDeepQuestionsCall),
    "scout-c-how-true": (CallType.SCOUT_C_HOW_TRUE, ScoutCHowTrueCall),
    "scout-c-how-false": (CallType.SCOUT_C_HOW_FALSE, ScoutCHowFalseCall),
    "scout-c-cruxes": (CallType.SCOUT_C_CRUXES, ScoutCCruxesCall),
    "scout-c-relevant-evidence": (
        CallType.SCOUT_C_RELEVANT_EVIDENCE,
        ScoutCRelevantEvidenceCall,
    ),
    "scout-c-stress-test-cases": (
        CallType.SCOUT_C_STRESS_TEST_CASES,
        ScoutCStressTestCasesCall,
    ),
    "scout-c-robustify": (CallType.SCOUT_C_ROBUSTIFY, ScoutCRobustifyCall),
    "scout-c-strengthen": (CallType.SCOUT_C_STRENGTHEN, ScoutCStrengthenCall),
}

CALL_TYPES = [
    "find-considerations",
    "assess",
    "web-research",
    "create-view",
    *_SCOUT_MAP.keys(),
]

DEFAULT_BUDGET = 3


def _tag_call_params(call: Call, skill: str) -> dict:
    """Origin metadata to merge into calls.call_params."""
    existing = call.call_params or {}
    return {
        **existing,
        "origin": "claude-code",
        "skill": skill,
    }


async def _dispatch(
    db: DB,
    call_type_str: str,
    question_id: str,
    *,
    budget: int,
    max_rounds: int | None,
) -> Call:
    """Create + run the appropriate CallRunner, returning the (saved) Call."""
    settings = get_settings()

    if call_type_str == "find-considerations":
        call = await db.create_call(CallType.FIND_CONSIDERATIONS, scope_page_id=question_id)
        call.call_params = _tag_call_params(call, "rumil-dispatch")
        await db.save_call(call)
        runner = FindConsiderationsCall(
            question_id,
            call,
            db,
            max_rounds=max_rounds or 5,
            fruit_threshold=DEFAULT_FRUIT_THRESHOLD,
        )
        await runner.run()
        return call

    if call_type_str == "assess":
        call = await db.create_call(CallType.ASSESS, scope_page_id=question_id)
        call.call_params = _tag_call_params(call, "rumil-dispatch")
        await db.save_call(call)
        cls = ASSESS_CALL_CLASSES[settings.assess_call_variant]
        runner = cls(question_id, call, db)
        await runner.run()
        return call

    if call_type_str == "web-research":
        call = await db.create_call(CallType.WEB_RESEARCH, scope_page_id=question_id)
        call.call_params = _tag_call_params(call, "rumil-dispatch")
        await db.save_call(call)
        runner = WebResearchCall(question_id, call, db)
        await runner.run()
        return call

    if call_type_str == "create-view":
        call = await db.create_call(CallType.CREATE_VIEW, scope_page_id=question_id)
        call.call_params = _tag_call_params(call, "rumil-dispatch")
        await db.save_call(call)
        runner = CreateViewCall(question_id, call, db)
        await runner.run()
        return call

    if call_type_str in _SCOUT_MAP:
        ct, cls = _SCOUT_MAP[call_type_str]
        call = await db.create_call(ct, scope_page_id=question_id)
        call.call_params = _tag_call_params(call, "rumil-dispatch")
        await db.save_call(call)
        runner = cls(question_id, call, db, max_rounds=max_rounds or 5)
        await runner.run()
        return call

    raise ValueError(f"unknown call type: {call_type_str}")


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
