"""Grounding feedback pipeline: improve workspace sourcing based on evaluation output."""

import asyncio
import logging
from collections.abc import Sequence
from pathlib import Path

import anthropic
from anthropic.types import ServerToolUseBlock, TextBlock

from claude_agent_sdk import AgentDefinition, tool
from pydantic import BaseModel, Field

from rumil.database import DB
from rumil.evaluate.explore import explore_page_impl
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
from rumil.moves.create_claim import CreateClaimPayload
from rumil.moves.create_claim import execute_with_source_creation
from rumil.moves.create_judgement import CreateJudgementForQuestionPayload
from rumil.moves.create_judgement import (
    execute_for_question as execute_create_judgement,
)
from rumil.moves.link_consideration import LinkConsiderationPayload
from rumil.moves.link_consideration import execute as execute_link_consideration
from rumil.moves.remove_link import RemoveLinkPayload
from rumil.moves.remove_link import execute as execute_remove_link
from rumil.sdk_agent import SdkAgentConfig, make_explore_tool, run_sdk_agent
from rumil.settings import get_settings
from rumil.tracing.broadcast import Broadcaster
from rumil.tracing.trace_events import (
    GroundingTasksGeneratedEvent,
    WebResearchCompleteEvent,
)
from rumil.tracing.tracer import CallTrace

log = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parents[3] / "prompts"
_TOOL_SERVER_NAME = "grounding-tools"
_WEB_SEARCH_SEMAPHORE = asyncio.Semaphore(10)


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
) -> Call:
    """Run the full grounding feedback pipeline and return the Call record."""
    question = await db.get_page(question_id)
    if question is None:
        raise ValueError(f'Question "{question_id}" not found')

    call = await db.create_call(
        call_type=CallType.GROUNDING_FEEDBACK,
        scope_page_id=question_id,
    )
    trace = CallTrace(call.id, db, broadcaster=broadcaster)
    await db.update_call_status(call.id, CallStatus.RUNNING)

    try:
        log.info("Stage 1: generating grounding tasks")
        tasks = await _generate_tasks(evaluation_text, question.headline, call, db)
        log.info("Stage 1 complete: %d tasks generated", len(tasks))

        await trace.record(
            GroundingTasksGeneratedEvent(
                task_count=len(tasks),
                tasks=[t.model_dump() for t in tasks],
            )
        )

        if not tasks:
            call.result_summary = (
                "No grounding tasks generated — evaluation found no actionable gaps."
            )
            call.status = CallStatus.COMPLETE
            await db.save_call(call)
            return call

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

        log.info("Stage 3: updating workspace")
        await _run_workspace_update(
            question=question,
            evaluation_text=evaluation_text,
            tasks=tasks,
            findings=findings,
            call=call,
            db=db,
            trace=trace,
            broadcaster=broadcaster,
        )
        log.info("Stage 3 complete")

        call.result_summary = (
            f"Grounding feedback complete: {len(tasks)} claims investigated, "
            f"{len(findings)} web research results collected."
        )
        call.status = CallStatus.COMPLETE
        await db.save_call(call)
    except Exception:
        log.exception("Grounding feedback pipeline failed")
        await db.update_call_status(call.id, CallStatus.FAILED)
        raise

    return call


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
    if result.data is None:
        return []
    task_list = GroundingTaskList.model_validate(result.data)
    return task_list.tasks


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


async def _build_update_user_message(
    question: Page,
    evaluation_text: str,
    tasks: Sequence[GroundingTask],
    findings: Sequence[tuple[GroundingTask, str]],
    call: Call,
    db: DB,
) -> str:
    """Build the user message for Stage 3.

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
            f"<claim_findings index=\"{i}\" claim=\"{task.claim}\">\n"
            f"Search task: {task.search_task}\n\n"
            f"{task_findings}\n"
            f"</claim_findings>"
        )
    findings_text = "\n\n".join(findings_text_parts)

    return (
        f"<briefing>\n"
        f"The following briefing was prepared from an evaluation of the "
        f"workspace's evidential grounding. It identifies claims with "
        f"grounding issues and lists the relevant workspace page IDs "
        f"you will need to navigate the graph.\n\n"
        f"{briefing}\n"
        f"</briefing>\n\n"
        f"<web_research_findings>\n"
        f"Web research agents were tasked with investigating the issues "
        f"outlined in the briefing above. Here is what they found. Use "
        f"the URLs in `source_urls` when creating claims.\n\n"
        f"{findings_text}\n"
        f"</web_research_findings>"
    )


def _make_grounding_tools(db: DB, call: Call) -> Sequence:
    """Create MCP tool definitions for the grounding feedback agent."""
    source_page_cache: dict[str, str] = {}

    explore_page = make_explore_tool(db)

    @tool(
        "create_claim",
        "Create a new claim with supporting reasoning and epistemic status. "
        "source_urls accepts URLs or page IDs — URLs are automatically scraped "
        "and turned into Source pages. Inline [url] citations in content are "
        "rewritten to [page_id] references. "
        "Use links to simultaneously link as a consideration on questions.",
        CreateClaimPayload.model_json_schema(),
    )
    async def create_claim(args: dict) -> dict:
        result = await execute_with_source_creation(
            args, call, db, source_page_cache
        )
        return {"content": [{"type": "text", "text": result.message}]}

    @tool(
        "link_consideration",
        "Link a claim to a question as a consideration with a strength "
        "rating (0-5) indicating how strongly it bears on the question.",
        LinkConsiderationPayload.model_json_schema(),
    )
    async def link_consideration(args: dict) -> dict:
        payload = LinkConsiderationPayload(**args)
        result = await execute_link_consideration(payload, call, db)
        return {"content": [{"type": "text", "text": result.message}]}

    @tool(
        "remove_link",
        "Remove a link between pages by its full UUID.",
        RemoveLinkPayload.model_json_schema(),
    )
    async def remove_link(args: dict) -> dict:
        payload = RemoveLinkPayload(**args)
        result = await execute_remove_link(payload, call, db)
        return {"content": [{"type": "text", "text": result.message}]}

    @tool(
        "create_judgement_for_question",
        "Create a judgement linked to a specific question (by ID). "
        "The judgement supersedes any prior judgement on that question. "
        "Include key_dependencies and sensitivity_analysis fields.",
        CreateJudgementForQuestionPayload.model_json_schema(),
    )
    async def create_judgement_for_question(args: dict) -> dict:
        payload = CreateJudgementForQuestionPayload(**args)
        result = await execute_create_judgement(payload, call, db)
        return {"content": [{"type": "text", "text": result.message}]}

    return [
        explore_page,
        create_claim,
        link_consideration,
        remove_link,
        create_judgement_for_question,
    ]


async def _run_workspace_update(
    question: Page,
    evaluation_text: str,
    tasks: Sequence[GroundingTask],
    findings: Sequence[tuple[GroundingTask, str]],
    call: Call,
    db: DB,
    trace: CallTrace,
    broadcaster: Broadcaster | None = None,
) -> None:
    """Stage 3: update workspace using Claude Agent SDK with MCP tools."""
    system_prompt = (
        (_PROMPTS_DIR / "preamble.md").read_text()
        + "\n\n"
        + (_PROMPTS_DIR / "grounding-feedback.md").read_text()
        + "\n\n"
        + (_PROMPTS_DIR / "citations.md").read_text()
    )

    tools = _make_grounding_tools(db, call)
    tool_fqnames = [f"mcp__{_TOOL_SERVER_NAME}__{t.name}" for t in tools]

    worker_prompt = (
        (_PROMPTS_DIR / "preamble.md").read_text()
        + "\n\n"
        + (_PROMPTS_DIR / "grounding-feedback.md").read_text()
        + "\n\n"
        + (_PROMPTS_DIR / "citations.md").read_text()
        + "\n\nYou are a grounding worker subagent. Complete the specific "
        "task assigned to you by the parent agent, then stop."
    )

    user_prompt = await _build_update_user_message(
        question, evaluation_text, tasks, findings, call, db
    )

    config = SdkAgentConfig(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        server_name=_TOOL_SERVER_NAME,
        mcp_tools=tools,
        call=call,
        call_type=CallType.GROUNDING_FEEDBACK,
        scope_page_id=question.id,
        db=db,
        trace=trace,
        broadcaster=broadcaster,
        disallowed_tools=("Write", "Edit", "Glob"),
        agents={
            "grounding_worker": AgentDefinition(
                description=(
                    "Worker agent for updating workspace grounding. Has the "
                    "same tools as the parent agent. Delegate specific claim "
                    "updates to this agent."
                ),
                prompt=worker_prompt,
                tools=tool_fqnames + ["Read", "Bash", "Grep"],
            ),
        },
    )

    try:
        await run_sdk_agent(config)
    except Exception:
        log.exception("Stage 3 workspace update agent failed")
        raise
