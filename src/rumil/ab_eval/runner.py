"""Orchestration for A/B evaluation: run agents, compare, report."""

import asyncio
import logging
import sys
import traceback
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from rumil.database import DB
from rumil.llm import LLMExchangeMetadata, text_call
from rumil.models import Call, CallStatus, CallType
from rumil.run_eval.agents import EVAL_AGENTS, EvalAgentSpec
from rumil.run_eval.runner import evaluate_run_with_agent
from rumil.settings import get_settings
from rumil.tracing.broadcast import Broadcaster
from rumil.tracing.trace_events import AgentStartedEvent
from rumil.tracing.tracer import CallTrace, reset_trace, set_trace

log = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parents[3] / "prompts"


def _frontend_trace_url(run_id: str, call_id: str | None = None) -> str:
    base = get_settings().frontend_url.rstrip("/")
    anchor = f"#call-{call_id[:8]}" if call_id else ""
    return f"{base}/traces/{run_id}{anchor}"


def _frontend_ab_eval_url(report_id: str) -> str:
    base = get_settings().frontend_url.rstrip("/")
    return f"{base}/ab-evals/{report_id}"


def _announce_call(label: str, run_id: str, call_id: str) -> None:
    """Print a trace URL for a freshly-created call, flushed immediately."""
    print(f"  {label}: {_frontend_trace_url(run_id, call_id)}", flush=True)


def _print_error(label: str, exc: BaseException) -> None:
    """Print an error to stderr so long-running eval runs surface failures."""
    print(
        f"  [error] {label}: {type(exc).__name__}: {exc}",
        file=sys.stderr,
        flush=True,
    )


def _print_exception_group(label: str, eg: BaseExceptionGroup) -> None:
    """Print every leaf exception from an ExceptionGroup."""
    for exc in eg.exceptions:
        if isinstance(exc, BaseExceptionGroup):
            _print_exception_group(label, exc)
        else:
            _print_error(label, exc)
            tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            print(tb, file=sys.stderr, flush=True)


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
    comparison_call_id: str = ""


async def _traced_text_call(
    db: DB,
    *,
    call_type: CallType,
    scope_page_id: str | None,
    system_prompt: str,
    user_message: str,
    phase: str,
    broadcaster: Broadcaster | None,
    announce_label: str | None = None,
) -> tuple[str, Call]:
    """Run a single-turn text LLM call wrapped in a Call + CallTrace.

    The call record is created before the LLM request is issued so the trace
    URL can be printed immediately (when *announce_label* is set). Exchanges
    and trace events are persisted against *db*.
    """
    call = await db.create_call(call_type=call_type, scope_page_id=scope_page_id)
    trace = CallTrace(call.id, db, broadcaster=broadcaster)
    await db.update_call_status(call.id, CallStatus.RUNNING)
    if announce_label:
        _announce_call(announce_label, db.run_id, call.id)

    token = set_trace(trace)
    try:
        await trace.record(
            AgentStartedEvent(
                system_prompt=system_prompt,
                user_message=user_message,
            )
        )
        response_text = await text_call(
            system_prompt=system_prompt,
            user_message=user_message,
            metadata=LLMExchangeMetadata(call_id=call.id, phase=phase),
            db=db,
        )
        call.status = CallStatus.COMPLETE
        call.completed_at = datetime.now(UTC)
        call.result_summary = response_text[:500]
        if trace.total_cost_usd > 0:
            call.cost_usd = trace.total_cost_usd
        await db.save_call(call)
    except Exception as exc:
        log.exception("Traced text call failed (call %s)", call.id)
        _print_error(f"{phase} (call {call.id[:8]})", exc)
        await db.update_call_status(call.id, CallStatus.FAILED)
        raise
    finally:
        reset_trace(token)

    return response_text, call


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


async def _run_comparison(
    spec: EvalAgentSpec,
    report_a: str,
    report_b: str,
    db: DB,
    scope_page_id: str | None,
    broadcaster: Broadcaster | None,
) -> tuple[str, str, Call]:
    """Run the comparison LLM call as a traced call. Returns (text, preference, call)."""
    comparison_prompt = (_PROMPTS_DIR / "ab-eval-comparison.md").read_text()
    user_message = (
        f"## Evaluation Dimension: {spec.display_name}\n\n"
        "## Run A Report\n\n"
        f"{report_a}\n\n"
        "---\n\n"
        "## Run B Report\n\n"
        f"{report_b}"
    )
    comparison_text, call = await _traced_text_call(
        db,
        call_type=CallType.AB_EVAL_COMPARISON,
        scope_page_id=scope_page_id,
        system_prompt=comparison_prompt,
        user_message=user_message,
        phase=f"ab_eval_comparison_{spec.name}",
        broadcaster=broadcaster,
        announce_label=f"compare {spec.name}",
    )
    return comparison_text, _extract_preference(comparison_text), call


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

    async def _eval_arm(run_id: str, question_id: str, label: str) -> tuple[str, Call]:
        arm_call = await db.create_call(
            call_type=CallType.AB_EVAL,
            scope_page_id=question_id,
        )
        _announce_call(f"{spec.name} {label}", db.run_id, arm_call.id)
        return await evaluate_run_with_agent(
            spec,
            run_id,
            question_id,
            db,
            broadcaster,
            call_type=CallType.AB_EVAL,
            label=label,
            all_agents=all_agents,
            call=arm_call,
        )

    try:
        async with asyncio.TaskGroup() as tg:
            task_a = tg.create_task(_eval_arm(run_id_a, question_id_a, "A"))
            task_b = tg.create_task(_eval_arm(run_id_b, question_id_b, "B"))
    except* Exception as eg:
        _print_exception_group(f"eval agent {spec.name}", eg)
        raise
    report_a, call_a = task_a.result()
    report_b, call_b = task_b.result()

    comparison, preference, comparison_call = await _run_comparison(
        spec,
        report_a,
        report_b,
        db,
        scope_page_id=question_id_a,
        broadcaster=broadcaster,
    )

    return ABEvalResult(
        agent_name=spec.name,
        report_a=report_a,
        report_b=report_b,
        comparison=comparison,
        preference=preference,
        call_id_a=call_a.id,
        call_id_b=call_b.id,
        comparison_call_id=comparison_call.id,
    )


async def _generate_overall_assessment(
    agent_reports: Sequence[tuple[EvalAgentSpec, str, str, str, str]],
    db: DB,
    scope_page_id: str | None,
    broadcaster: Broadcaster | None,
) -> tuple[str, Call]:
    """Generate an LLM-written overall assessment. Returns (text, call)."""
    final_prompt = (_PROMPTS_DIR / "ab-eval-final-report.md").read_text()
    sections: list[str] = []
    for spec, _ra, _rb, comparison, preference in agent_reports:
        sections.append(f"### {spec.display_name}\n\n**Preference: {preference}**\n\n{comparison}")
    user_message = "\n\n---\n\n".join(sections)
    return await _traced_text_call(
        db,
        call_type=CallType.AB_EVAL_SUMMARY,
        scope_page_id=scope_page_id,
        system_prompt=final_prompt,
        user_message=user_message,
        phase="ab_eval_overall_assessment",
        broadcaster=broadcaster,
        announce_label="overall assessment",
    )


async def run_ab_eval(
    run_id_a: str,
    run_id_b: str,
    db: DB,
    broadcaster: Broadcaster | None = None,
    agents_override: Sequence[EvalAgentSpec] | None = None,
) -> str:
    """Run all evaluation agents concurrently and persist the aggregate report.

    Returns the ID of the saved ``ab_eval_reports`` row.
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

    agents = agents_override if agents_override is not None else EVAL_AGENTS

    results: list[ABEvalResult] = []
    try:
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
    except* Exception as eg:
        _print_exception_group("ab_eval", eg)
        raise
    results = [t.result() for t in tasks]

    agent_reports = [
        (spec, r.report_a, r.report_b, r.comparison, r.preference)
        for spec, r in zip(agents, results)
    ]

    overall_assessment, overall_call = await _generate_overall_assessment(
        agent_reports,
        db,
        scope_page_id=question_id_a,
        broadcaster=broadcaster,
    )

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
            "comparison_call_id": r.comparison_call_id,
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
        overall_assessment_call_id=overall_call.id,
    )

    print(_frontend_ab_eval_url(report_id), flush=True)
    return report_id
