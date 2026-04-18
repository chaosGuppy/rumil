"""Orchestration for single-run evaluation: run agents and report."""

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from claude_agent_sdk import tool as sdk_tool

from rumil.database import DB
from rumil.evaluate.explore import explore_page_impl
from rumil.llm import Tool, text_call
from rumil.models import Call, CallStatus, CallType
from rumil.run_eval.agents import EVAL_AGENTS, EvalAgentSpec
from rumil.run_eval.baselines import (
    SingleCallBaselineResult,
    render_baseline_view,
    run_single_call_baseline,
)
from rumil.run_eval.quality_control import (
    QualityControlFinding,
    cap_findings,
    format_findings_markdown,
    parse_findings_from_report,
    severity_to_score,
)
from rumil.run_eval.report import format_run_eval_report, save_run_eval_report
from rumil.sdk_agent import SdkAgentConfig, run_sdk_agent
from rumil.settings import get_settings
from rumil.tracing.broadcast import Broadcaster
from rumil.tracing.tracer import CallTrace
from rumil.workspace_exploration import make_explore_subgraph_tool, make_load_page_tool

log = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parents[3] / "prompts"
_TOOL_SERVER_NAME = "run-eval-tools"


def wrap_as_mcp_tool(llm_tool: Tool):
    """Wrap a rumil.llm.Tool as an SdkMcpTool for the Claude Agent SDK."""

    @sdk_tool(llm_tool.name, llm_tool.description, llm_tool.input_schema)
    async def wrapped(args: dict) -> dict:
        result = await llm_tool.fn(args)
        return {"content": [{"type": "text", "text": result}]}

    return wrapped


async def _record_eval_reputation(
    parent_db: DB,
    *,
    agent_name: str,
    run_id: str,
    question_id: str,
    call_id: str,
) -> None:
    """Record a reputation event for a completed eval-agent assessment.

    Endogenous substrate hook (see CLAUDE.md and
    marketplace-thread/07-feedback.md). The eval agent's free-form text
    report is not itself a numeric score, so we record a completion-sentinel
    (1.0) here and let AB-eval's comparison step emit the real preference
    score. Callers can aggregate these per (source, dimension) at query time
    without losing the raw signal. Never collapse sources at write time.
    """
    run_row = await parent_db.get_run(run_id)
    config = (run_row or {}).get("config") or {}
    orchestrator = config.get("orchestrator") if isinstance(config, dict) else None

    task_shape: dict | None = None
    question = await parent_db.get_page(question_id)
    if question is not None:
        extra = question.extra or {}
        if isinstance(extra.get("task_shape"), dict):
            task_shape = extra["task_shape"]

    await parent_db.record_reputation_event(
        source="eval_agent",
        dimension=agent_name,
        score=1.0,
        orchestrator=orchestrator,
        task_shape=task_shape,
        source_call_id=call_id,
        extra={"subject_run_id": run_id},
    )


async def _record_qc_findings_reputation(
    parent_db: DB,
    *,
    findings: Sequence[QualityControlFinding],
    run_id: str,
    question_id: str,
    call_id: str,
) -> None:
    """Emit one negative-score reputation event per QC finding.

    Each finding's severity maps to a negative score (see
    ``severity_to_score``) so the ``quality_control`` dimension's mean
    score tracks "how many quality deficits did this run ship". Raw events
    remain per-finding — no collapsing at write time. The findings
    themselves are also persisted on the run_eval_report JSONB so the
    dashboard can render the full detail, not just the score.
    """
    if not findings:
        return
    run_row = await parent_db.get_run(run_id)
    config = (run_row or {}).get("config") or {}
    orchestrator = config.get("orchestrator") if isinstance(config, dict) else None

    task_shape: dict | None = None
    question = await parent_db.get_page(question_id)
    if question is not None:
        extra = question.extra or {}
        if isinstance(extra.get("task_shape"), dict):
            task_shape = extra["task_shape"]

    for finding in findings:
        await parent_db.record_reputation_event(
            source="eval_agent",
            dimension="quality_control",
            score=severity_to_score(finding.severity),
            orchestrator=orchestrator,
            task_shape=task_shape,
            source_call_id=call_id,
            extra={
                "subject_run_id": run_id,
                "kind": finding.kind,
                "severity": finding.severity.value,
                "page_ids": list(finding.page_ids),
                "evidence": finding.evidence,
                "suggested_fix": finding.suggested_fix,
            },
        )


def build_system_prompt(
    spec: EvalAgentSpec,
    all_agents: Sequence[EvalAgentSpec] = (),
) -> str:
    """Concatenate preamble + agent-specific prompt.

    When *all_agents* is provided, ``{other_dimensions}`` in the prompt is
    replaced with a bullet list of the other agents' display names.  This
    lets the general-quality prompt automatically exclude dimensions covered
    by sibling agents.
    """
    preamble = (_PROMPTS_DIR / "preamble.md").read_text()
    agent_prompt = (_PROMPTS_DIR / spec.prompt_file).read_text()
    if "{other_dimensions}" in agent_prompt:
        others = [
            f"- **{s.display_name}** (evaluated separately)"
            for s in all_agents
            if s.name != spec.name
        ]
        if others:
            replacement = "\n".join(others)
        else:
            replacement = "(No other dimensions are being evaluated in this run.)"
        agent_prompt = agent_prompt.replace("{other_dimensions}", replacement)
    return preamble + "\n\n" + agent_prompt


@dataclass
class RunEvalResult:
    """Result from a single evaluation agent assessing one run."""

    agent_name: str
    display_name: str
    report: str
    call_id: str
    findings: list[QualityControlFinding] = field(default_factory=list)


async def evaluate_run_with_agent(
    spec: EvalAgentSpec,
    run_id: str,
    question_id: str,
    parent_db: DB,
    broadcaster: Broadcaster | None,
    call_type: CallType = CallType.RUN_EVAL,
    label: str | None = None,
    all_agents: Sequence[EvalAgentSpec] = (),
    call: Call | None = None,
) -> tuple[str, Call]:
    """Run one evaluation agent against a staged run.

    Returns (report_text, call). The optional ``label`` is prepended to the
    user prompt (e.g. "A" or "B" in the AB eval context) — when *None* the
    prompt simply says "Evaluate the run". Pass an existing ``call`` to reuse
    a Call record created by the caller (so its trace URL can be surfaced
    before the LLM work begins).
    """
    eval_db = await DB.create(
        run_id=run_id,
        prod=parent_db._prod,
        project_id=parent_db.project_id,
        staged=True,
    )

    if call is None:
        call = await parent_db.create_call(
            call_type=call_type,
            scope_page_id=question_id,
        )
    trace = CallTrace(call.id, parent_db, broadcaster=broadcaster)
    await parent_db.update_call_status(call.id, CallStatus.RUNNING)

    initial_context = await explore_page_impl(
        question_id,
        eval_db,
        highlight_run_id=run_id,
    )
    explore_llm_tool = make_explore_subgraph_tool(
        eval_db,
        trace,
        questions_only=False,
        highlight_run_id=run_id,
    )
    load_page_llm_tool = make_load_page_tool(
        eval_db,
        trace,
        highlight_run_id=run_id,
    )
    mcp_tools = [
        wrap_as_mcp_tool(explore_llm_tool),
        wrap_as_mcp_tool(load_page_llm_tool),
    ]

    system_prompt = build_system_prompt(spec, all_agents=all_agents)
    if label:
        run_intro = f"Evaluate Run {label} for the question with ID `{question_id}`."
    else:
        run_intro = f"Evaluate the run for the question with ID `{question_id}`."
    user_prompt = (
        f"{run_intro}\n\n"
        "Focus on items marked [ADDED BY THIS RUN] -- these are the pages and "
        "links created by this run.\n\n"
        f"Here is the local graph around the root question:\n\n{initial_context}"
    )

    allowed = [
        f"mcp__{_TOOL_SERVER_NAME}__{t.name}" for t in [explore_llm_tool, load_page_llm_tool]
    ] + list(spec.extra_tools)

    config = SdkAgentConfig(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        server_name=_TOOL_SERVER_NAME,
        mcp_tools=mcp_tools,
        call=call,
        call_type=call_type,
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
        call.completed_at = datetime.now(UTC)
        call.result_summary = report_text[:500]
        if trace.total_cost_usd > 0:
            call.cost_usd = trace.total_cost_usd
        await parent_db.save_call(call)
        try:
            await _record_eval_reputation(
                parent_db,
                agent_name=spec.name,
                run_id=run_id,
                question_id=question_id,
                call_id=call.id,
            )
        except Exception:
            log.exception(
                "Reputation hook failed for eval agent %s on run %s",
                spec.name,
                run_id,
            )
        if spec.name == "quality_control":
            try:
                raw_findings = parse_findings_from_report(report_text)
                findings = cap_findings(raw_findings)
                await _record_qc_findings_reputation(
                    parent_db,
                    findings=findings,
                    run_id=run_id,
                    question_id=question_id,
                    call_id=call.id,
                )
            except Exception:
                log.exception(
                    "QC findings hook failed for run %s",
                    run_id,
                )
    except Exception:
        log.exception(
            "Eval agent %s failed for run %s",
            spec.name,
            run_id,
        )
        await parent_db.update_call_status(call.id, CallStatus.FAILED)
        raise

    return report_text, call


async def _generate_run_assessment(
    agent_reports: Sequence[tuple[EvalAgentSpec, str]],
) -> str:
    """Generate an LLM-written overall assessment from per-dimension reports."""
    final_prompt = (_PROMPTS_DIR / "run-eval-final-report.md").read_text()
    sections: list[str] = []
    for spec, report in agent_reports:
        sections.append(f"### {spec.display_name}\n\n{report}")
    user_message = "\n\n---\n\n".join(sections)
    return await text_call(system_prompt=final_prompt, user_message=user_message)


async def _maybe_run_single_call_baseline(
    question_id: str,
    db: DB,
    broadcaster: Broadcaster | None,
) -> SingleCallBaselineResult | None:
    """Fire the single-call baseline if enabled in settings.

    Opt-in via ``settings.eval_include_single_call_baseline``. Keeps the
    comparison flow minimal — we just record the baseline output alongside
    agent reports so humans can inspect it.
    """
    settings = get_settings()
    if not settings.eval_include_single_call_baseline:
        return None
    try:
        return await run_single_call_baseline(
            db,
            question_id,
            model=settings.single_call_baseline_model,
            broadcaster=broadcaster,
        )
    except Exception:
        log.exception("Single-call baseline failed for question %s", question_id)
        return None


async def run_run_eval(
    run_id: str,
    db: DB,
    broadcaster: Broadcaster | None = None,
    agents_override: Sequence[EvalAgentSpec] | None = None,
) -> Path:
    """Run all evaluation agents concurrently against a single staged run.

    Returns the path to the saved report file.
    """
    run = await db.get_run(run_id)
    if not run:
        raise ValueError(f"Run {run_id} not found")

    question_id = run.get("question_id")
    if not question_id:
        raise ValueError(f"Run {run_id} has no question_id")

    agents = agents_override if agents_override is not None else EVAL_AGENTS

    print(f"\nRun Evaluation: {run_id[:8]}")
    print(f"Question: {question_id}")
    print(f"Running {len(agents)} evaluation agents concurrently...\n")

    baseline_result = await _maybe_run_single_call_baseline(
        question_id,
        db,
        broadcaster,
    )
    if baseline_result is not None:
        print(
            f"Single-call baseline: call {baseline_result.call_id[:8] if baseline_result.call_id else '??'}"
            f" ({baseline_result.input_tokens}/{baseline_result.output_tokens} tokens,"
            f" ${baseline_result.cost_usd:.4f})"
        )

    async with asyncio.TaskGroup() as tg:
        tasks = [
            tg.create_task(
                evaluate_run_with_agent(
                    spec,
                    run_id,
                    question_id,
                    db,
                    broadcaster,
                    all_agents=agents,
                )
            )
            for spec in agents
        ]
    eval_results = [
        RunEvalResult(
            agent_name=spec.name,
            display_name=spec.display_name,
            report=t.result()[0],
            call_id=t.result()[1].id,
            findings=(
                cap_findings(parse_findings_from_report(t.result()[0]))
                if spec.name == "quality_control"
                else []
            ),
        )
        for spec, t in zip(agents, tasks)
    ]

    agent_reports: list[tuple[EvalAgentSpec, str]] = [
        (spec, r.report) for spec, r in zip(agents, eval_results)
    ]

    overall_assessment = await _generate_run_assessment(agent_reports)
    aggregate = format_run_eval_report(agent_reports, run_id, overall_assessment)
    qc_findings_all: list[QualityControlFinding] = [f for r in eval_results for f in r.findings]
    if qc_findings_all:
        aggregate += (
            "\n---\n\n"
            "# Quality Control Findings (structured)\n\n"
            f"{format_findings_markdown(qc_findings_all)}\n"
        )
    if baseline_result is not None:
        aggregate += (
            "\n---\n\n"
            "# Single-Call Baseline (comparator)\n\n"
            f"**Model:** `{baseline_result.model}`  \n"
            f"**Call:** `{baseline_result.call_id}`  \n"
            f"**Tokens:** {baseline_result.input_tokens} in / "
            f"{baseline_result.output_tokens} out  \n"
            f"**Cost:** ${baseline_result.cost_usd:.4f}\n\n"
            "> Produced in a single LLM call given the same context the "
            "orchestrator would have seen. Compare manually against the "
            "orchestrator-produced view to sanity-check that the orchestrator "
            "is net-positive.\n\n"
            f"{render_baseline_view(baseline_result.view, baseline_result.response_text)}\n"
        )
    report_path = save_run_eval_report(aggregate, run_id)

    dimension_rows = [
        {
            "name": r.agent_name,
            "display_name": r.display_name,
            "report": r.report,
            "call_id": r.call_id,
            "findings": [f.model_dump(mode="json") for f in r.findings],
        }
        for r in eval_results
    ]
    report_id = await db.save_run_eval_report(
        run_id=run_id,
        question_id=question_id,
        overall_assessment=overall_assessment,
        dimension_reports=dimension_rows,
    )
    log.info("Run eval report saved to DB: %s", report_id)

    print(f"\nReport saved to: {report_path}")
    print("\n" + "=" * 60)
    print(aggregate)

    return report_path
