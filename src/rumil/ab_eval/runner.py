"""Orchestration for A/B evaluation: run one agent per dimension that produces a direct comparison."""

import asyncio
import logging
import sys
import traceback
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from rumil.ab_eval.arm_tools import (
    make_arm_explore_subgraph_tool,
    make_arm_load_page_tool,
    make_arm_search_tool,
)
from rumil.database import DB
from rumil.llm import LLMExchangeMetadata, text_call
from rumil.models import Call, CallStatus, CallType
from rumil.prompts import PROMPTS_DIR as _PROMPTS_DIR
from rumil.run_eval.agents import EVAL_AGENTS, EvalAgentSpec
from rumil.run_eval.runner import wrap_as_mcp_tool
from rumil.run_eval.seed import build_eval_seed_context
from rumil.sdk_agent import SdkAgentConfig, run_sdk_agent
from rumil.settings import get_settings
from rumil.tracing.broadcast import Broadcaster
from rumil.tracing.trace_events import AgentStartedEvent
from rumil.tracing.tracer import CallTrace, reset_trace, set_trace

log = logging.getLogger(__name__)

_TOOL_SERVER_NAME = "ab-eval-tools"


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
    """Result from a single evaluation agent that directly compared A and B."""

    agent_name: str
    preference: str
    report: str
    call_id: str = ""


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
    """Run a single-turn text LLM call wrapped in a Call + CallTrace."""
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


def _build_comparison_system_prompt(
    spec: EvalAgentSpec,
    all_agents: Sequence[EvalAgentSpec],
) -> str:
    """Build system prompt = preamble + ab-eval-dimension shell with dimension body."""
    preamble = (_PROMPTS_DIR / "preamble.md").read_text()
    shell = (_PROMPTS_DIR / "ab-eval-dimension.md").read_text()
    dim_prompt = (_PROMPTS_DIR / spec.prompt_file).read_text()
    if "{other_dimensions}" in dim_prompt:
        others = [
            f"- **{s.display_name}** (evaluated separately)"
            for s in all_agents
            if s.name != spec.name
        ]
        replacement = (
            "\n".join(others)
            if others
            else "(No other dimensions are being evaluated in this run.)"
        )
        dim_prompt = dim_prompt.replace("{other_dimensions}", replacement)
    shell = shell.replace("{dimension_task}", dim_prompt)
    return preamble + "\n\n" + shell


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
    """Run one evaluation agent that directly compares A and B.

    Builds two staged DBs (one per arm), wraps the exploration tools so the
    agent selects an arm per call, seeds the context with both arms'
    1-hop subgraphs side-by-side, and runs a single agent to produce the
    comparison report. The preference label is extracted from the report.
    """
    eval_db_a = await DB.create(
        run_id=run_id_a,
        prod=db._prod,
        project_id=db.project_id,
        staged=True,
    )
    eval_db_b = await DB.create(
        run_id=run_id_b,
        prod=db._prod,
        project_id=db.project_id,
        staged=True,
    )

    call = await db.create_call(
        call_type=CallType.AB_EVAL,
        scope_page_id=question_id_a,
    )
    trace = CallTrace(call.id, db, broadcaster=broadcaster)
    await db.update_call_status(call.id, CallStatus.RUNNING)
    _announce_call(f"compare {spec.name}", db.run_id, call.id)

    seed_a = await build_eval_seed_context(
        question_id_a,
        eval_db_a,
        highlight_run_id=run_id_a,
    )
    seed_b = await build_eval_seed_context(
        question_id_b,
        eval_db_b,
        highlight_run_id=run_id_b,
    )

    explore_tool = make_arm_explore_subgraph_tool(
        eval_db_a,
        eval_db_b,
        trace,
        run_id_a=run_id_a,
        run_id_b=run_id_b,
    )
    load_page_tool = make_arm_load_page_tool(
        eval_db_a,
        eval_db_b,
        trace,
        run_id_a=run_id_a,
        run_id_b=run_id_b,
    )
    search_tool = make_arm_search_tool(eval_db_a, eval_db_b, trace)
    mcp_tools = [
        wrap_as_mcp_tool(explore_tool),
        wrap_as_mcp_tool(load_page_tool),
        wrap_as_mcp_tool(search_tool),
    ]

    system_prompt = _build_comparison_system_prompt(spec, all_agents)
    user_prompt = (
        f"Compare Run A and Run B on the dimension **{spec.display_name}**.\n\n"
        f"Run A question ID: `{question_id_a}`\n"
        f"Run B question ID: `{question_id_b}`\n\n"
        "Focus on items marked `[ADDED BY THIS RUN]` in each arm's seed -- "
        "those are the pages and links created by that run. Use the "
        "`explore_subgraph`, `load_page`, and `search_workspace` tools "
        "with the `arm` field set to 'A' or 'B' to drill into either "
        "workspace. Running the same `search_workspace` query against "
        "both arms is a good way to check whether a topic was covered "
        "on both sides.\n\n"
        f"## Run A seed\n\n{seed_a}\n\n"
        "---\n\n"
        f"## Run B seed\n\n{seed_b}"
    )

    allowed = [
        f"mcp__{_TOOL_SERVER_NAME}__{t.name}" for t in [explore_tool, load_page_tool, search_tool]
    ] + list(spec.extra_tools)

    config = SdkAgentConfig(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        server_name=_TOOL_SERVER_NAME,
        mcp_tools=mcp_tools,
        call=call,
        call_type=CallType.AB_EVAL,
        scope_page_id=question_id_a,
        db=db,
        trace=trace,
        broadcaster=broadcaster,
        allowed_tools=allowed,
        disallowed_tools=["Write", "Edit", "Glob"],
    )

    try:
        result = await run_sdk_agent(config)
        report_text = "\n\n".join(result.all_assistant_text)
        call.status = CallStatus.COMPLETE
        call.completed_at = datetime.now(UTC)
        call.result_summary = report_text[:500]
        if trace.total_cost_usd > 0:
            call.cost_usd = trace.total_cost_usd
        await db.save_call(call)
    except Exception as exc:
        log.exception("AB eval agent %s failed (runs %s vs %s)", spec.name, run_id_a, run_id_b)
        _print_error(f"ab eval {spec.name}", exc)
        await db.update_call_status(call.id, CallStatus.FAILED)
        raise

    return ABEvalResult(
        agent_name=spec.name,
        preference=_extract_preference(report_text),
        report=report_text,
        call_id=call.id,
    )


async def _generate_overall_assessment(
    agent_reports: Sequence[tuple[EvalAgentSpec, str, str]],
    db: DB,
    scope_page_id: str | None,
    broadcaster: Broadcaster | None,
) -> tuple[str, Call]:
    """Generate an LLM-written overall assessment. Returns (text, call)."""
    final_prompt = (_PROMPTS_DIR / "ab-eval-final-report.md").read_text()
    dim_list = ", ".join(spec.display_name for spec, _, _ in agent_reports)
    sections: list[str] = [f"The dimensions evaluated in this run are: {dim_list}."]
    for spec, report, preference in agent_reports:
        sections.append(f"### {spec.display_name}\n\n**Preference: {preference}**\n\n{report}")
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

    agent_reports = [(spec, r.report, r.preference) for spec, r in zip(agents, results)]

    if len(agent_reports) == 1:
        spec, _, preference = agent_reports[0]
        overall_assessment = (
            f"Only one dimension was evaluated in this run: **{spec.display_name}** "
            f"(preference: {preference}). See the dimension report below for the full comparison."
        )
        overall_call_id: str | None = None
    else:
        overall_assessment, overall_call = await _generate_overall_assessment(
            agent_reports,
            db,
            scope_page_id=question_id_a,
            broadcaster=broadcaster,
        )
        overall_call_id = overall_call.id

    dimension_rows = [
        {
            "name": spec.name,
            "display_name": spec.display_name,
            "preference": r.preference,
            "report": r.report,
            "call_id": r.call_id,
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
        overall_assessment_call_id=overall_call_id,
    )

    print(_frontend_ab_eval_url(report_id), flush=True)
    return report_id
