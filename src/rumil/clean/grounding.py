"""Grounding feedback pipeline: improve workspace sourcing based on evaluation output."""

import asyncio
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import anthropic
from anthropic.types import ServerToolUseBlock, TextBlock

from pydantic import BaseModel, Field

from rumil.moves.base import HEADLINE_DESCRIPTION

from rumil.clean.common import (
    UpdateOperation,
    UpdatePlan,
    execute_update_plan,
    generate_abstracts,
    log_plan,
    normalize_plan,
    save_checkpoint,
)
from rumil.database import DB
from rumil.llm import (
    LLMExchangeMetadata,
    call_api,
    structured_call,
)
from rumil.models import (
    Call,
    CallStatus,
    CallType,
    Page,
)
from claude_agent_sdk import AgentDefinition, tool

from rumil.moves.create_claim import ensure_source_page, execute_with_source_creation
from rumil.explore_tool import make_explore_tool
from rumil.sdk_agent import SdkAgentConfig, run_sdk_agent
from rumil.settings import get_settings
from rumil.tracing.broadcast import Broadcaster
from rumil.tracing.trace_events import (
    GroundingTasksGeneratedEvent,
    UpdatePlanCreatedEvent,
    WebResearchCompleteEvent,
)
from rumil.tracing.tracer import CallTrace

log = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parents[3] / "prompts"
_TOOL_SERVER_NAME = "grounding-identify"
_PLAN_SERVER_NAME = "grounding-plan"
_WEB_SEARCH_SEMAPHORE = asyncio.Semaphore(10)


class _CreateSourceInput(BaseModel):
    url: str = Field(description="HTTP URL to scrape")


class _CreateClaimInput(BaseModel):
    headline: str = Field(description=HEADLINE_DESCRIPTION)
    content: str = Field(
        description=(
            "Full claim text. Cite sources inline using [url] syntax "
            "(e.g. [https://example.com]) — these are automatically "
            "rewritten to source page short IDs."
        )
    )
    credence: int = Field(description="Credence level 1-9")
    robustness: int = Field(description="Robustness level 1-5")
    supersedes: str = Field(
        default="",
        description=(
            "8-char short ID of the old claim to replace. "
            "Consideration links are automatically copied from the old claim."
        ),
    )
    source_urls: list[str] = Field(
        default_factory=list,
        description=(
            "HTTP URLs of sources. Each is automatically scraped and "
            "turned into a source page with a CITES link."
        ),
    )


def _make_create_source_tool(call: Call, db: DB):
    """MCP tool: scrape a URL and create a SOURCE page."""
    source_cache: dict[str, str] = {}

    @tool(
        "create_source",
        "Scrape a URL and create a source page in the workspace. "
        "Returns the 8-char short ID of the new source page.",
        _CreateSourceInput.model_json_schema(),
    )
    async def create_source(args: dict) -> dict:
        url = args["url"]
        page_id = await ensure_source_page(url, call, db, source_cache)
        if page_id is None:
            return {"content": [{"type": "text", "text": f"Failed to scrape {url}"}]}
        return {
            "content": [
                {"type": "text", "text": f"Source page created: [{page_id[:8]}]"}
            ]
        }

    return create_source


def _make_create_claim_tool(call: Call, db: DB):
    """MCP tool: create a claim page with automatic source handling."""
    source_cache: dict[str, str] = {}

    @tool(
        "create_claim",
        "Create a claim page that supersedes an old one. Automatically "
        "scrapes source URLs, rewrites [url] inline citations to "
        "[shortid], and copies consideration links from the old claim.",
        _CreateClaimInput.model_json_schema(),
    )
    async def create_claim(args: dict) -> dict:
        result = await execute_with_source_creation(args, call, db, source_cache)
        return {"content": [{"type": "text", "text": result.message}]}

    return create_claim


_CLAIM_UPDATER_PROMPT = (
    "You replace a single claim in a research workspace with a new, "
    "better-grounded version backed by primary sources.\n\n"
    "IMPORTANT: The new claim must be a complete, standalone page — "
    "written from scratch as if the old claim did not exist. Do not "
    "write diffs, summaries of changes, or references to 'the previous "
    "version'. The new page should read as an authoritative, self-contained "
    "analysis.\n\n"
    "Your workflow:\n"
    "1. Use `explore_page` to read the old claim and understand its "
    "context.\n"
    "2. Use `create_claim` to write the replacement claim. Pass:\n"
    "   - `supersedes`: the old claim's short ID (consideration links "
    "are automatically copied from the old claim)\n"
    "   - `source_urls`: HTTP URLs from the findings (automatically "
    "scraped and turned into source pages)\n"
    "   - `content`: the full claim text — cite sources inline "
    "using [url] syntax (e.g. [https://example.com]), these are "
    "automatically rewritten to source page short IDs\n"
    "   - `credence` and `robustness`: updated epistemic status\n\n"
    "Do NOT describe claims as 'confirmed', 'empirically grounded', "
    "'verified', or similar — neither in the headline nor the content. "
    "Let the source citations and the credence/robustness fields speak "
    "for themselves. Present the evidence and analysis neutrally.\n\n"
    "Be thorough but concise. Focus on factual accuracy and proper "
    "source attribution."
)


class GroundingTask(BaseModel):
    claim: str = Field(description="The claim text being investigated")
    grounding_issue: str = Field(
        description="What is wrong with the grounding of this claim"
    )
    search_task: str = Field(
        description=(
            "Focused description of what to search for on the web "
            "to find sources that verify or refute this claim"
        )
    )


class GroundingTaskList(BaseModel):
    tasks: list[GroundingTask]


async def run_grounding_feedback(
    question_id: str,
    evaluation_text: str,
    db: DB,
    *,
    broadcaster: Broadcaster | None = None,
    from_stage: int = 1,
    prior_checkpoints: dict | None = None,
) -> Call:
    """Run the full grounding feedback pipeline and return the Call record.

    *from_stage* (1–5) lets you resume from an intermediate stage.
    When resuming, *prior_checkpoints* supplies the outputs of earlier
    stages (loaded from a previous call's ``call_params["checkpoints"]``).
    """
    question = await db.get_page(question_id)
    if question is None:
        raise ValueError(f'Question "{question_id}" not found')

    call = await db.create_call(
        call_type=CallType.GROUNDING_FEEDBACK,
        scope_page_id=question_id,
    )
    trace = CallTrace(call.id, db, broadcaster=broadcaster)
    await db.update_call_status(call.id, CallStatus.RUNNING)

    cp = prior_checkpoints or {}

    try:
        if from_stage <= 1:
            log.info("Stage 1: generating grounding tasks")
            tasks = await _generate_tasks(evaluation_text, question.headline, call, db)
            log.info("Stage 1 complete: %d tasks generated", len(tasks))
            await trace.record(
                GroundingTasksGeneratedEvent(
                    task_count=len(tasks),
                    tasks=[t.model_dump() for t in tasks],
                )
            )
        else:
            tasks = [GroundingTask(**t) for t in cp["tasks"]]
            log.info("Stage 1: loaded %d tasks from prior run", len(tasks))

        _save_checkpoint(call, "tasks", [t.model_dump() for t in tasks])
        await db.save_call(call)

        if not tasks:
            call.result_summary = (
                "No grounding tasks generated — evaluation found no actionable gaps."
            )
            call.status = CallStatus.COMPLETE
            await db.save_call(call)
            return call

        if from_stage <= 2:
            log.info("Stage 2: running web research for %d tasks", len(tasks))
            findings = await _run_web_research(tasks, call, db)
            log.info("Stage 2 complete: %d findings collected", len(findings))
            await trace.record(
                WebResearchCompleteEvent(
                    task_count=len(findings),
                    findings=[
                        {"claim": task.claim, "findings_length": len(text)}
                        for task, text in findings
                    ],
                )
            )
        else:
            findings = [
                (GroundingTask(**f["task"]), f["findings_text"]) for f in cp["findings"]
            ]
            log.info("Stage 2: loaded %d findings from prior run", len(findings))

        _save_checkpoint(
            call,
            "findings",
            [
                {"task": task.model_dump(), "findings_text": text}
                for task, text in findings
            ],
        )
        await db.save_call(call)

        if from_stage <= 3:
            log.info("Stage 3: planning updates")
            plan = await _plan_updates(
                question=question,
                evaluation_text=evaluation_text,
                tasks=tasks,
                findings=findings,
                call=call,
                db=db,
                trace=trace,
                broadcaster=broadcaster,
            )
            log.info(
                "Stage 3 complete: %d waves, %d operations",
                len(plan.waves),
                sum(len(w) for w in plan.waves),
            )
            await trace.record(
                UpdatePlanCreatedEvent(
                    wave_count=len(plan.waves),
                    operation_count=sum(len(w) for w in plan.waves),
                    waves=[[op.model_dump() for op in wave] for wave in plan.waves],
                )
            )
        else:
            plan = UpdatePlan(
                waves=[
                    [UpdateOperation(**op) for op in wave] for wave in cp["update_plan"]
                ]
            )
            log.info(
                "Stage 3: loaded plan from prior run (%d waves, %d ops)",
                len(plan.waves),
                sum(len(w) for w in plan.waves),
            )

        _save_checkpoint(
            call,
            "update_plan",
            [[op.model_dump() for op in wave] for wave in plan.waves],
        )
        await db.save_call(call)

        total_ops = sum(len(w) for w in plan.waves)
        if total_ops == 0:
            call.result_summary = (
                "No updates planned — findings did not warrant changes."
            )
            call.status = CallStatus.COMPLETE
            await db.save_call(call)
            return call

        log.info("Stage 4: executing update plan")
        await execute_update_plan(plan, call, db, trace)
        log.info("Stage 4 complete")

        log.info("Stage 5: generating abstracts and embeddings")
        await generate_abstracts(call, db)
        log.info("Stage 5 complete")

        call.result_summary = (
            f"Grounding feedback complete: {len(tasks)} claims investigated, "
            f"{len(findings)} web research results collected, "
            f"{total_ops} updates executed in {len(plan.waves)} waves."
        )
        call.status = CallStatus.COMPLETE
        await db.save_call(call)
    except Exception:
        log.exception("Grounding feedback pipeline failed")
        await db.update_call_status(call.id, CallStatus.FAILED)
        raise

    return call


def _save_checkpoint(call: Call, key: str, data: Any) -> None:
    save_checkpoint(call, key, data)


async def _generate_tasks(
    evaluation_text: str,
    question_headline: str,
    call: Call,
    db: DB,
) -> Sequence[GroundingTask]:
    """Stage 1: parse evaluation output and produce web research tasks."""
    system_prompt = (_PROMPTS_DIR / "grounding-task-generation.md").read_text()
    user_message = (
        f"Question under investigation: {question_headline}\n\n"
        f"Evaluation output:\n\n{evaluation_text}"
    )

    meta = LLMExchangeMetadata(
        call_id=call.id,
        phase="grounding_task_generation",
    )
    result = await structured_call(
        system_prompt,
        user_message=user_message,
        response_model=GroundingTaskList,
        metadata=meta,
        db=db,
    )
    if result.parsed is None:
        return []
    return result.parsed.tasks


async def _run_web_search_task(
    task: GroundingTask,
    task_index: int,
    call: Call,
    db: DB,
) -> str:
    """Run a single web search task and return findings as text."""
    settings = get_settings()
    client = anthropic.AsyncAnthropic(api_key=settings.require_anthropic_key())
    system_prompt = (_PROMPTS_DIR / "grounding-web-research.md").read_text()

    server_tools: list[dict] = [
        {
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": 5,
        }
    ]

    user_message = (
        f"Claim to investigate: {task.claim}\n\n"
        f"Grounding issue: {task.grounding_issue}\n\n"
        f"Search task: {task.search_task}"
    )
    messages: list[dict] = [{"role": "user", "content": user_message}]
    max_rounds = 2 if settings.is_smoke_test else 3

    text_parts: list[str] = []
    async with _WEB_SEARCH_SEMAPHORE:
        for round_num in range(max_rounds):
            meta = LLMExchangeMetadata(
                call_id=call.id,
                phase=f"web_research_task_{task_index}",
                round_num=round_num,
                user_message=user_message if round_num == 0 else None,
            )
            api_resp = await call_api(
                client,
                settings.model,
                system_prompt,
                messages,
                server_tools,
                metadata=meta,
                db=db,
            )
            response = api_resp.message

            for block in response.content:
                if isinstance(block, TextBlock):
                    text_parts.append(block.text)

            messages.append({"role": "assistant", "content": response.content})

            has_server_tools = any(
                isinstance(b, ServerToolUseBlock) for b in response.content
            )
            if response.stop_reason == "end_turn" or not has_server_tools:
                break

    return "\n\n".join(text_parts)


async def _run_web_research(
    tasks: Sequence[GroundingTask],
    call: Call,
    db: DB,
) -> Sequence[tuple[GroundingTask, str]]:
    """Stage 2: run web research for all tasks concurrently."""

    async def _run_one(task: GroundingTask, index: int) -> tuple[GroundingTask, str]:
        findings = await _run_web_search_task(task, index, call, db)
        return (task, findings)

    results = await asyncio.gather(*[_run_one(task, i) for i, task in enumerate(tasks)])
    return list(results)


async def _build_identification_user_message(
    question: Page,
    evaluation_text: str,
    tasks: Sequence[GroundingTask],
    findings: Sequence[tuple[GroundingTask, str]],
    call: Call,
    db: DB,
) -> str:
    """Build the user message for Stage 3 (affected page identification).

    An LLM call synthesises the evaluation output into a concise briefing
    with page IDs and evidence chains.  The raw web research findings are
    then appended via template so they are never truncated.
    """
    system_prompt = (_PROMPTS_DIR / "grounding-update-briefing.md").read_text()

    tasks_text_parts: list[str] = []
    for i, task in enumerate(tasks, 1):
        tasks_text_parts.append(
            f"{i}. **{task.claim}**\n"
            f"   Grounding issue: {task.grounding_issue}\n"
            f"   Search task: {task.search_task}"
        )
    tasks_text = "\n\n".join(tasks_text_parts)

    llm_input = (
        f'Target question: "{question.headline}" (ID: `{question.id}`)\n\n'
        f"## Evaluation output\n\n{evaluation_text}\n\n"
        f"## Claims selected for web research\n\n{tasks_text}"
    )

    settings = get_settings()
    client = anthropic.AsyncAnthropic(api_key=settings.require_anthropic_key())
    meta = LLMExchangeMetadata(
        call_id=call.id,
        phase="grounding_briefing_generation",
    )
    api_resp = await call_api(
        client,
        settings.model,
        system_prompt,
        [{"role": "user", "content": llm_input}],
        metadata=meta,
        db=db,
    )
    briefing_parts: list[str] = []
    for block in api_resp.message.content:
        if isinstance(block, TextBlock):
            briefing_parts.append(block.text)
    briefing = "\n".join(briefing_parts)

    findings_text_parts: list[str] = []
    for i, (task, task_findings) in enumerate(findings, 1):
        findings_text_parts.append(
            f'<claim_findings index="{i}" claim="{task.claim}">\n'
            f"Search task: {task.search_task}\n\n"
            f"{task_findings}\n"
            f"</claim_findings>"
        )
    findings_text = "\n\n".join(findings_text_parts)

    return (
        f"<briefing>\n"
        f"The following briefing was prepared from an evaluation of the "
        f"workspace's evidential grounding. It identifies claims with "
        f"grounding issues and lists the relevant workspace page IDs.\n\n"
        f"{briefing}\n"
        f"</briefing>\n\n"
        f"<web_research_findings>\n"
        f"Web research agents investigated the issues outlined in the "
        f"briefing above. Here is what they found.\n\n"
        f"{findings_text}\n"
        f"</web_research_findings>"
    )


async def _plan_updates(
    question: Page,
    evaluation_text: str,
    tasks: Sequence[GroundingTask],
    findings: Sequence[tuple[GroundingTask, str]],
    call: Call,
    db: DB,
    trace: CallTrace,
    broadcaster: Broadcaster | None = None,
) -> UpdatePlan:
    """Stage 3: agent explores graph, updates leaf claims via subagents,
    then returns a propagation plan for upstream updates."""
    settings = get_settings()
    budget = settings.grounding_update_budget

    plan_prompt = (_PROMPTS_DIR / "grounding-plan-updates.md").read_text()
    wave_prompt = (_PROMPTS_DIR / "update-waves.md").read_text()
    system_prompt = (
        (_PROMPTS_DIR / "preamble.md").read_text()
        + "\n\n"
        + plan_prompt
        + "\n\n"
        + wave_prompt.replace("{budget}", str(budget))
    )

    explore_tool = make_explore_tool(db)
    create_source_tool = _make_create_source_tool(call, db)
    create_claim_tool = _make_create_claim_tool(call, db)

    plan_tools = [explore_tool, create_source_tool, create_claim_tool]
    subagent_tool_fqnames = [
        f"mcp__{_PLAN_SERVER_NAME}__{t.name}" for t in [explore_tool, create_claim_tool]
    ]
    all_tool_fqnames = [f"mcp__{_PLAN_SERVER_NAME}__{t.name}" for t in plan_tools]

    user_prompt = await _build_identification_user_message(
        question, evaluation_text, tasks, findings, call, db
    )

    config = SdkAgentConfig(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        server_name=_PLAN_SERVER_NAME,
        mcp_tools=plan_tools,
        call=call,
        call_type=CallType.GROUNDING_FEEDBACK,
        scope_page_id=question.id,
        db=db,
        trace=trace,
        broadcaster=broadcaster,
        agents={
            "claim-updater": AgentDefinition(
                description=(
                    "Updates a single claim with source grounding. "
                    "Give it the claim page ID and relevant findings."
                ),
                prompt=_CLAIM_UPDATER_PROMPT,
                tools=subagent_tool_fqnames,
            ),
        },
        allowed_tools=all_tool_fqnames + ["Agent", "Bash", "Read"],
        disallowed_tools=(),
        output_format={
            "type": "json_schema",
            "schema": UpdatePlan.model_json_schema(),
        },
    )

    result = await run_sdk_agent(config)

    if result.structured_output is None:
        log.warning("Stage 3 agent returned no structured output")
        return UpdatePlan(waves=[])

    plan = UpdatePlan.model_validate(normalize_plan(result.structured_output))
    log_plan(plan)
    return plan
