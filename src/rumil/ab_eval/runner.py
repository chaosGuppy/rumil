"""Orchestration for A/B evaluation: run agents, compare, report."""

import asyncio
import logging
import random
import sys
import traceback
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from rumil.database import DB
from rumil.llm import LLMExchangeMetadata, structured_call, text_call
from rumil.models import Call, CallStatus, CallType
from rumil.run_eval.agents import EVAL_AGENTS, EvalAgentSpec
from rumil.run_eval.runner import evaluate_run_with_agent
from rumil.settings import get_settings
from rumil.tracing.broadcast import Broadcaster
from rumil.tracing.tracer import CallTrace, reset_trace, set_trace

log = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parents[3] / "prompts"


def _frontend_trace_url(call_id: str) -> str:
    base = get_settings().frontend_url.rstrip("/")
    return f"{base}/traces/{call_id}"


def _frontend_ab_eval_url(report_id: str) -> str:
    base = get_settings().frontend_url.rstrip("/")
    return f"{base}/ab-evals/{report_id}"


def _announce_call(label: str, call_id: str) -> None:
    """Print a trace URL for a freshly-created call, flushed immediately."""
    print(f"  {label}: {_frontend_trace_url(call_id)}", flush=True)


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
    """Result from a single evaluation agent comparing runs A and B.

    `preference` is expressed in the caller's A/B frame (i.e. it refers to the
    original `run_id_a` / `run_id_b` passed in), regardless of which run
    occupied the "Run A" slot in the comparison prompt. `a_was_first` records
    the randomized slot assignment so the effect can be audited.
    """

    agent_name: str
    report_a: str
    report_b: str
    comparison: str
    preference: str
    a_was_first: bool = True
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
        _announce_call(announce_label, call.id)

    token = set_trace(trace)
    try:
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


PreferenceLabel = Literal[
    "A strongly preferred",
    "A somewhat preferred",
    "A slightly preferred",
    "Approximately indifferent between A and B",
    "B slightly preferred",
    "B somewhat preferred",
    "B strongly preferred",
]

_PREFERENCE_LABELS: Sequence[PreferenceLabel] = [
    "A strongly preferred",
    "A somewhat preferred",
    "A slightly preferred",
    "Approximately indifferent between A and B",
    "B slightly preferred",
    "B somewhat preferred",
    "B strongly preferred",
]

_SWAP_MAP: dict[PreferenceLabel, PreferenceLabel] = {
    "A strongly preferred": "B strongly preferred",
    "A somewhat preferred": "B somewhat preferred",
    "A slightly preferred": "B slightly preferred",
    "Approximately indifferent between A and B": "Approximately indifferent between A and B",
    "B slightly preferred": "A slightly preferred",
    "B somewhat preferred": "A somewhat preferred",
    "B strongly preferred": "A strongly preferred",
}


def _deswap_preference(prompt_preference: PreferenceLabel, a_was_first: bool) -> PreferenceLabel:
    """Map a preference from the comparison-prompt's A/B frame to the caller's A/B frame.

    When `a_was_first` is True, the caller's run A occupied the "Run A" slot, so
    the preference is already in the caller's frame. When False, the runs were
    swapped in the prompt, so we flip A<->B labels to restore the caller's frame.
    """
    if a_was_first:
        return prompt_preference
    return _SWAP_MAP[prompt_preference]


class _PreferenceExtraction(BaseModel):
    """Structured output for extracting a preference label from a comparison."""

    preference: PreferenceLabel = Field(
        description=(
            "The preference rating stated in the comparison text. Must be "
            "exactly one of the seven allowed labels."
        )
    )


_PREFERENCE_EXTRACTION_SYSTEM = (
    "You extract a single preference rating from an A/B research-run comparison. "
    "You will be given the comparison text. Return the one preference label that "
    "the author of the comparison stated as their final rating. The label must be "
    "exactly one of: " + ", ".join(f'"{label}"' for label in _PREFERENCE_LABELS) + "."
)


async def _extract_preference_structured(
    comparison_text: str,
    db: DB,
    metadata: LLMExchangeMetadata,
) -> PreferenceLabel:
    """Extract the preference rating from comparison text via structured LLM call."""
    result = await structured_call(
        _PREFERENCE_EXTRACTION_SYSTEM,
        f"Comparison text:\n\n{comparison_text}",
        response_model=_PreferenceExtraction,
        metadata=metadata,
        db=db,
    )
    if result.parsed is None:
        raise ValueError("Structured preference extraction returned no parseable output")
    return result.parsed.preference


_PREFERENCE_SCORES_FOR_A: dict[str, float] = {
    "A strongly preferred": 3.0,
    "A somewhat preferred": 2.0,
    "A slightly preferred": 1.0,
    "Approximately indifferent between A and B": 0.0,
    "B slightly preferred": -1.0,
    "B somewhat preferred": -2.0,
    "B strongly preferred": -3.0,
}


def preference_to_score(preference: str, for_arm: str) -> float | None:
    """Map a 7-label AB-eval preference to a symmetric numeric score.

    Returns a float in [-3, +3] from arm's perspective: +3 = "our arm
    strongly preferred", -3 = "the other arm strongly preferred". Returns
    None for "Could not determine preference" so callers can skip recording.
    The mapping is deliberately symmetric so consumers can aggregate without
    double-counting direction.
    """
    if preference not in _PREFERENCE_SCORES_FOR_A:
        return None
    a_score = _PREFERENCE_SCORES_FOR_A[preference]
    if for_arm == "A":
        return a_score
    if for_arm == "B":
        return -a_score
    raise ValueError(f"for_arm must be 'A' or 'B', got {for_arm!r}")


async def _record_ab_preference_reputation(
    db: DB,
    *,
    agent_name: str,
    preference: str,
    run_id_a: str,
    run_id_b: str,
    question_id_a: str,
    question_id_b: str,
    comparison_call_id: str,
) -> None:
    """Record a reputation_event per arm for a 7-label AB-eval preference.

    Never collapse sources or dimensions: A and B each get their own event
    tagged with source='eval_agent', dimension=<agent_name>, score derived
    symmetrically from the preference label. See
    marketplace-thread/13-reputation-governance.md.
    """
    score_a = preference_to_score(preference, "A")
    score_b = preference_to_score(preference, "B")
    if score_a is None or score_b is None:
        return

    run_row_a = await db.get_run(run_id_a)
    run_row_b = await db.get_run(run_id_b)
    orch_a = _extract_orchestrator(run_row_a)
    orch_b = _extract_orchestrator(run_row_b)

    task_shape_a = await _extract_task_shape(db, question_id_a)
    task_shape_b = await _extract_task_shape(db, question_id_b)

    extra_base = {"preference_label": preference}

    await db.record_reputation_event(
        source="eval_agent",
        dimension=agent_name,
        score=score_a,
        orchestrator=orch_a,
        task_shape=task_shape_a,
        source_call_id=comparison_call_id,
        extra={**extra_base, "subject_run_id": run_id_a, "arm": "A"},
    )
    await db.record_reputation_event(
        source="eval_agent",
        dimension=agent_name,
        score=score_b,
        orchestrator=orch_b,
        task_shape=task_shape_b,
        source_call_id=comparison_call_id,
        extra={**extra_base, "subject_run_id": run_id_b, "arm": "B"},
    )


def _extract_orchestrator(run_row: dict | None) -> str | None:
    if not run_row:
        return None
    config = run_row.get("config") or {}
    if isinstance(config, dict):
        val = config.get("orchestrator")
        return val if isinstance(val, str) else None
    return None


async def _extract_task_shape(db: DB, question_id: str | None) -> dict | None:
    if not question_id:
        return None
    page = await db.get_page(question_id)
    if page is None:
        return None
    shape = (page.extra or {}).get("task_shape")
    return shape if isinstance(shape, dict) else None


async def _run_comparison(
    spec: EvalAgentSpec,
    report_a: str,
    report_b: str,
    db: DB,
    scope_page_id: str | None,
    broadcaster: Broadcaster | None,
    rng: random.Random | None = None,
) -> tuple[str, PreferenceLabel, Call, bool]:
    """Run the comparison LLM call as a traced call.

    Randomizes which of the caller's runs (A or B) is presented as "Run A" in
    the prompt to neutralize LLM position bias, then de-swaps the returned
    preference back to the caller's A/B frame.

    Returns (comparison_text, preference, call, a_was_first). The preference is
    expressed in the caller's frame; `a_was_first` records the slot assignment.
    """
    chooser = rng if rng is not None else random
    a_was_first = chooser.random() < 0.5
    prompt_report_a = report_a if a_was_first else report_b
    prompt_report_b = report_b if a_was_first else report_a

    comparison_prompt = (_PROMPTS_DIR / "ab-eval-comparison.md").read_text()
    user_message = (
        f"## Evaluation Dimension: {spec.display_name}\n\n"
        "## Run A Report\n\n"
        f"{prompt_report_a}\n\n"
        "---\n\n"
        "## Run B Report\n\n"
        f"{prompt_report_b}"
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
    prompt_preference = await _extract_preference_structured(
        comparison_text,
        db,
        LLMExchangeMetadata(
            call_id=call.id,
            phase=f"ab_eval_preference_extract_{spec.name}",
        ),
    )
    preference = _deswap_preference(prompt_preference, a_was_first)
    return comparison_text, preference, call, a_was_first


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
        _announce_call(f"{spec.name} {label}", arm_call.id)
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

    comparison, preference, comparison_call, a_was_first = await _run_comparison(
        spec,
        report_a,
        report_b,
        db,
        scope_page_id=question_id_a,
        broadcaster=broadcaster,
    )

    try:
        await _record_ab_preference_reputation(
            db,
            agent_name=spec.name,
            preference=preference,
            run_id_a=run_id_a,
            run_id_b=run_id_b,
            question_id_a=question_id_a,
            question_id_b=question_id_b,
            comparison_call_id=comparison_call.id,
        )
    except Exception:
        log.exception(
            "Reputation hook failed for AB-eval comparison %s",
            comparison_call.id,
        )

    return ABEvalResult(
        agent_name=spec.name,
        report_a=report_a,
        report_b=report_b,
        comparison=comparison,
        preference=preference,
        a_was_first=a_was_first,
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
