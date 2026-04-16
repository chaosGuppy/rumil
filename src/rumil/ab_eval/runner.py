"""Orchestration for A/B evaluation: run agents, compare, report."""

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from rumil.ab_eval.report import format_aggregate_report, save_ab_report
from rumil.database import DB
from rumil.llm import text_call
from rumil.models import CallType
from rumil.run_eval.agents import EvalAgentSpec, EVAL_AGENTS
from rumil.run_eval.runner import evaluate_run_with_agent
from rumil.tracing.broadcast import Broadcaster

log = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parents[3] / "prompts"


@dataclass
class ABEvalResult:
    """Result from a single evaluation agent comparing runs A and B."""

    agent_name: str
    report_a: str
    report_b: str
    comparison: str
    preference: str
    call_id_a: str = ""
    call_id_b: str = ""


async def _run_comparison(
    spec: EvalAgentSpec,
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
    spec: EvalAgentSpec,
    run_id_a: str,
    run_id_b: str,
    question_id_a: str,
    question_id_b: str,
    db: DB,
    broadcaster: Broadcaster | None = None,
    all_agents: Sequence[EvalAgentSpec] = (),
) -> ABEvalResult:
    """Run one evaluation agent: evaluate A and B concurrently, then compare."""
    log.info("Starting AB eval agent: %s", spec.display_name)

    async with asyncio.TaskGroup() as tg:
        task_a = tg.create_task(
            evaluate_run_with_agent(
                spec,
                run_id_a,
                question_id_a,
                db,
                broadcaster,
                call_type=CallType.AB_EVAL,
                label="A",
                all_agents=all_agents,
            )
        )
        task_b = tg.create_task(
            evaluate_run_with_agent(
                spec,
                run_id_b,
                question_id_b,
                db,
                broadcaster,
                call_type=CallType.AB_EVAL,
                label="B",
                all_agents=all_agents,
            )
        )
    report_a, call_a = task_a.result()
    report_b, call_b = task_b.result()
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
        call_id_a=call_a.id,
        call_id_b=call_b.id,
    )


async def _generate_overall_assessment(
    agent_reports: Sequence[tuple[EvalAgentSpec, str, str, str, str]],
) -> str:
    """Generate an LLM-written overall assessment from per-dimension comparisons."""
    final_prompt = (_PROMPTS_DIR / "ab-eval-final-report.md").read_text()
    sections: list[str] = []
    for spec, _ra, _rb, comparison, preference in agent_reports:
        sections.append(
            f"### {spec.display_name}\n\n**Preference: {preference}**\n\n{comparison}"
        )
    user_message = "\n\n---\n\n".join(sections)
    return await text_call(system_prompt=final_prompt, user_message=user_message)


async def run_ab_eval(
    run_id_a: str,
    run_id_b: str,
    db: DB,
    broadcaster: Broadcaster | None = None,
    agents_override: Sequence[EvalAgentSpec] | None = None,
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
    if not question_id_a:
        raise ValueError(f"Run {run_id_a} has no question_id")
    if not question_id_b:
        raise ValueError(f"Run {run_id_b} has no question_id")
    if question_id_a != question_id_b:
        log.info(
            "Runs target different questions: %s vs %s (each arm "
            "will be evaluated against its own root question)",
            question_id_a,
            question_id_b,
        )

    agents = agents_override if agents_override is not None else EVAL_AGENTS

    print(f"\nA/B Evaluation: {run_id_a[:8]} vs {run_id_b[:8]}")
    print(f"Question A: {question_id_a}")
    if question_id_b != question_id_a:
        print(f"Question B: {question_id_b}")
    print(f"Running {len(agents)} evaluation agents concurrently...\n")

    results: list[ABEvalResult] = []
    async with asyncio.TaskGroup() as tg:
        tasks = [
            tg.create_task(
                run_single_eval_agent(
                    spec,
                    run_id_a,
                    run_id_b,
                    question_id_a,
                    question_id_b,
                    db,
                    broadcaster,
                    all_agents=agents,
                )
            )
            for spec in agents
        ]
    results = [t.result() for t in tasks]

    agent_reports = [
        (spec, r.report_a, r.report_b, r.comparison, r.preference)
        for spec, r in zip(agents, results)
    ]

    overall_assessment = await _generate_overall_assessment(agent_reports)
    aggregate = format_aggregate_report(
        agent_reports, run_id_a, run_id_b, overall_assessment
    )
    report_path = save_ab_report(aggregate, run_id_a, run_id_b)

    dimension_rows = [
        {
            "name": spec.name,
            "display_name": spec.display_name,
            "preference": r.preference,
            "report_a": r.report_a,
            "report_b": r.report_b,
            "comparison": r.comparison,
            "call_id_a": r.call_id_a,
            "call_id_b": r.call_id_b,
        }
        for spec, r in zip(agents, results)
    ]
    report_id = await db.save_ab_eval_report(
        run_id_a=run_id_a,
        run_id_b=run_id_b,
        question_id_a=question_id_a,
        question_id_b=question_id_b,
        overall_assessment=overall_assessment,
        dimension_reports=dimension_rows,
    )
    log.info("AB eval report saved to DB: %s", report_id)

    print(f"\nReport saved to: {report_path}")
    print("\n" + "=" * 60)
    print(aggregate)

    return report_path
