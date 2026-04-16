"""Orchestration for single-run evaluation: run agents and report."""

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from claude_agent_sdk import tool as sdk_tool

from rumil.run_eval.agents import EvalAgentSpec, EVAL_AGENTS
from rumil.run_eval.report import format_run_eval_report, save_run_eval_report
from rumil.database import DB
from rumil.evaluate.explore import explore_page_impl
from rumil.llm import Tool, text_call
from rumil.workspace_exploration import make_explore_subgraph_tool, make_load_page_tool
from rumil.models import Call, CallStatus, CallType
from rumil.sdk_agent import SdkAgentConfig, run_sdk_agent
from rumil.tracing.broadcast import Broadcaster
from rumil.tracing.tracer import CallTrace

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
    if "{other_dimensions}" in agent_prompt and all_agents:
        others = [
            f"- **{s.display_name}** (evaluated separately)"
            for s in all_agents
            if s.name != spec.name
        ]
        agent_prompt = agent_prompt.replace("{other_dimensions}", "\n".join(others))
    return preamble + "\n\n" + agent_prompt


@dataclass
class RunEvalResult:
    """Result from a single evaluation agent assessing one run."""

    agent_name: str
    display_name: str
    report: str
    call_id: str


async def evaluate_run_with_agent(
    spec: EvalAgentSpec,
    run_id: str,
    question_id: str,
    parent_db: DB,
    broadcaster: Broadcaster | None,
    call_type: CallType = CallType.RUN_EVAL,
    label: str | None = None,
    all_agents: Sequence[EvalAgentSpec] = (),
) -> tuple[str, Call]:
    """Run one evaluation agent against a staged run.

    Returns (report_text, call). The optional ``label`` is prepended to the
    user prompt (e.g. "A" or "B" in the AB eval context) — when *None* the
    prompt simply says "Evaluate the run".
    """
    eval_db = await DB.create(
        run_id=run_id,
        prod=parent_db._prod,
        project_id=parent_db.project_id,
        staged=True,
    )

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
        f"mcp__{_TOOL_SERVER_NAME}__{t.name}"
        for t in [explore_llm_tool, load_page_llm_tool]
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
        )
        for spec, t in zip(agents, tasks)
    ]

    agent_reports: list[tuple[EvalAgentSpec, str]] = [
        (spec, r.report) for spec, r in zip(agents, eval_results)
    ]

    overall_assessment = await _generate_run_assessment(agent_reports)
    aggregate = format_run_eval_report(agent_reports, run_id, overall_assessment)
    report_path = save_run_eval_report(aggregate, run_id)

    dimension_rows = [
        {
            "name": r.agent_name,
            "display_name": r.display_name,
            "report": r.report,
            "call_id": r.call_id,
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
