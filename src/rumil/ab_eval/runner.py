"""Orchestration for A/B evaluation: run agents, compare, report."""

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from rumil.ab_eval.agents import ABEvalAgentSpec, EVAL_AGENTS
from rumil.ab_eval.report import format_aggregate_report, save_ab_report
from rumil.database import DB
from rumil.explore_tool import make_explore_tool
from rumil.evaluate.explore import explore_page_impl
from rumil.llm import text_call
from rumil.models import Call, CallStatus, CallType
from rumil.sdk_agent import SdkAgentConfig, run_sdk_agent
from rumil.tracing.broadcast import Broadcaster
from rumil.tracing.tracer import CallTrace

log = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parents[3] / "prompts"
_TOOL_SERVER_NAME = "ab-eval-tools"


@dataclass
class ABEvalResult:
    """Result from a single evaluation agent comparing runs A and B."""

    agent_name: str
    report_a: str
    report_b: str
    comparison: str
    preference: str


def _build_system_prompt(spec: ABEvalAgentSpec) -> str:
    """Concatenate preamble + agent-specific prompt."""
    preamble = (_PROMPTS_DIR / "preamble.md").read_text()
    agent_prompt = (_PROMPTS_DIR / spec.prompt_file).read_text()
    return preamble + "\n\n" + agent_prompt


async def _run_arm_evaluation(
    spec: ABEvalAgentSpec,
    arm_label: str,
    run_id: str,
    question_id: str,
    parent_db: DB,
    broadcaster: Broadcaster | None,
) -> tuple[str, Call]:
    """Run one evaluation agent against a single arm. Returns (report_text, call)."""
    arm_db = await DB.create(
        run_id=run_id,
        prod=parent_db._prod,
        project_id=parent_db.project_id,
        staged=True,
    )

    call = await parent_db.create_call(
        call_type=CallType.AB_EVAL,
        scope_page_id=question_id,
    )
    trace = CallTrace(call.id, parent_db, broadcaster=broadcaster)
    await parent_db.update_call_status(call.id, CallStatus.RUNNING)

    initial_context = await explore_page_impl(
        question_id,
        arm_db,
        highlight_run_id=run_id,
    )
    explore_tool = make_explore_tool(arm_db, highlight_run_id=run_id)

    system_prompt = _build_system_prompt(spec)
    user_prompt = (
        f"Evaluate Run {arm_label} for the question with ID `{question_id}`.\n\n"
        "Focus on items marked [ADDED BY THIS RUN] -- these are the pages and "
        "links created by this run.\n\n"
        f"Here is the local graph around the root question:\n\n{initial_context}"
    )

    explore_fqname = f"mcp__{_TOOL_SERVER_NAME}__explore_page"
    allowed = [explore_fqname] + list(spec.extra_tools)

    config = SdkAgentConfig(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        server_name=_TOOL_SERVER_NAME,
        mcp_tools=[explore_tool],
        call=call,
        call_type=CallType.AB_EVAL,
        scope_page_id=question_id,
        db=parent_db,
        trace=trace,
        broadcaster=broadcaster,
        allowed_tools=allowed,
        disallowed_tools=["Write", "Edit", "Glob"],
    )

    try:
        result = await run_sdk_agent(config)
        report_text = "\n\n".join(result.all_assistant_text)
        call.status = CallStatus.COMPLETE
        call.result_summary = report_text[:500]
        await parent_db.save_call(call)
    except Exception:
        log.exception(
            "AB eval agent %s failed on arm %s",
            spec.name,
            arm_label,
        )
        await parent_db.update_call_status(call.id, CallStatus.FAILED)
        raise

    return report_text, call


async def _run_comparison(
    spec: ABEvalAgentSpec,
    report_a: str,
    report_b: str,
) -> tuple[str, str]:
    """Run the comparison LLM call. Returns (comparison_text, preference)."""
    comparison_prompt = (_PROMPTS_DIR / "ab-eval-comparison.md").read_text()
    user_message = (
        f"## Evaluation Dimension: {spec.display_name}\n\n"
        "## Run A Report\n\n"
        f"{report_a}\n\n"
        "---\n\n"
        "## Run B Report\n\n"
        f"{report_b}"
    )
    comparison_text = await text_call(
        system_prompt=comparison_prompt,
        user_message=user_message,
    )

    preference = _extract_preference(comparison_text)
    return comparison_text, preference


_PREFERENCE_LABELS = [
    "A strongly preferred",
    "A somewhat preferred",
    "A slightly preferred",
    "Approximately indifferent between A and B",
    "B slightly preferred",
    "B somewhat preferred",
    "B strongly preferred",
]


def _extract_preference(text: str) -> str:
    """Extract the preference rating from comparison text."""
    text_lower = text.lower()
    for label in _PREFERENCE_LABELS:
        if label.lower() in text_lower:
            return label
    return "Could not determine preference"


async def run_single_eval_agent(
    spec: ABEvalAgentSpec,
    run_id_a: str,
    run_id_b: str,
    question_id: str,
    db: DB,
    broadcaster: Broadcaster | None = None,
) -> ABEvalResult:
    """Run one evaluation agent: evaluate A and B concurrently, then compare."""
    log.info("Starting AB eval agent: %s", spec.display_name)

    async with asyncio.TaskGroup() as tg:
        task_a = tg.create_task(
            _run_arm_evaluation(
                spec,
                "A",
                run_id_a,
                question_id,
                db,
                broadcaster,
            )
        )
        task_b = tg.create_task(
            _run_arm_evaluation(
                spec,
                "B",
                run_id_b,
                question_id,
                db,
                broadcaster,
            )
        )
    report_a, _ = task_a.result()
    report_b, _ = task_b.result()
    log.info("Agent %s: both arm evaluations complete", spec.name)

    comparison, preference = await _run_comparison(spec, report_a, report_b)
    log.info(
        "Agent %s: comparison complete — %s",
        spec.name,
        preference,
    )

    return ABEvalResult(
        agent_name=spec.name,
        report_a=report_a,
        report_b=report_b,
        comparison=comparison,
        preference=preference,
    )


async def run_ab_eval(
    run_id_a: str,
    run_id_b: str,
    db: DB,
    broadcaster: Broadcaster | None = None,
) -> Path:
    """Run all evaluation agents concurrently and produce the aggregate report.

    Returns the path to the saved report file.
    """
    run_a = await db.get_run(run_id_a)
    run_b = await db.get_run(run_id_b)
    if not run_a:
        raise ValueError(f"Run {run_id_a} not found")
    if not run_b:
        raise ValueError(f"Run {run_id_b} not found")

    question_id_a = run_a.get("question_id")
    question_id_b = run_b.get("question_id")
    if question_id_a != question_id_b:
        log.warning(
            "Runs target different questions: %s vs %s",
            question_id_a,
            question_id_b,
        )
    question_id = question_id_a or question_id_b
    if not question_id:
        raise ValueError("Neither run has a question_id")

    print(f"\nA/B Evaluation: {run_id_a[:8]} vs {run_id_b[:8]}")
    print(f"Question: {question_id}")
    print(f"Running {len(EVAL_AGENTS)} evaluation agents concurrently...\n")

    results: list[ABEvalResult] = []
    async with asyncio.TaskGroup() as tg:
        tasks = [
            tg.create_task(
                run_single_eval_agent(
                    spec,
                    run_id_a,
                    run_id_b,
                    question_id,
                    db,
                    broadcaster,
                )
            )
            for spec in EVAL_AGENTS
        ]
    results = [t.result() for t in tasks]

    agent_reports = [
        (spec, r.report_a, r.report_b, r.comparison, r.preference)
        for spec, r in zip(EVAL_AGENTS, results)
    ]
    aggregate = format_aggregate_report(agent_reports, run_id_a, run_id_b)
    report_path = save_ab_report(aggregate, run_id_a, run_id_b)

    print(f"\nReport saved to: {report_path}")
    print("\n" + "=" * 60)
    print(aggregate)

    return report_path
