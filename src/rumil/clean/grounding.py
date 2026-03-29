"""Grounding feedback pipeline: improve workspace sourcing based on evaluation output."""

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import anthropic
from anthropic.types import ServerToolUseBlock, TextBlock

from pydantic import BaseModel, Field

from rumil.calls.call_registry import ASSESS_CALL_CLASSES
from rumil.calls.common import (
    ABSTRACT_INSTRUCTION,
    PageSummaryItem,
    save_page_abstracts,
)
from rumil.context import build_embedding_based_context
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
    ConsiderationDirection,
    LinkType,
    Page,
    PageLayer,
    PageLink,
    PageType,
)
from rumil.moves.base import write_page_file
from rumil.sdk_agent import SdkAgentConfig, make_explore_tool, run_sdk_agent
from rumil.settings import get_settings
from rumil.tracing.broadcast import Broadcaster
from rumil.tracing.trace_events import (
    AffectedPagesIdentifiedEvent,
    ClaimReassessedEvent,
    GroundingTasksGeneratedEvent,
    ReassessTriggeredEvent,
    UpdateSubgraphComputedEvent,
    WebResearchCompleteEvent,
)
from rumil.tracing.tracer import CallTrace

log = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parents[3] / "prompts"
_TOOL_SERVER_NAME = "grounding-identify"
_WEB_SEARCH_SEMAPHORE = asyncio.Semaphore(10)
_UPDATE_SEMAPHORE = asyncio.Semaphore(5)


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


class AffectedPage(BaseModel):
    page_id: str = Field(
        description="8-char short ID of the directly affected page"
    )
    findings_summary: str = Field(
        description=(
            "Concise summary of the web research findings that "
            "contradict or bolster this specific page. Include "
            "relevant URLs."
        )
    )


class AffectedPageList(BaseModel):
    affected_pages: list[AffectedPage] = Field(
        description=(
            "Pages directly affected by the web research findings. "
            "Only include pages whose content is directly contradicted, "
            "refined, or bolstered by the findings. Do not include "
            "transitively affected pages (e.g. a judgement that cites "
            "an affected claim)."
        )
    )


class ReassessedClaim(BaseModel):
    headline: str = Field(description="New headline for the claim (10-15 words)")
    content: str = Field(description="Full standalone content of the replacement claim")
    epistemic_status: float = Field(description="Confidence 0.0-1.0")
    epistemic_type: str = Field(description="Nature of uncertainty")


@dataclass
class UpdateNode:
    """A node in the update subgraph."""

    page_id: str
    page: Page
    node_type: str  # "claim" or "question"
    question_id: str | None = None  # for question nodes, the question page ID
    input_ids: set[str] = field(default_factory=set)
    findings: str | None = None  # non-None if directly affected


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

        log.info("Stage 3: identifying affected pages")
        affected = await _identify_affected_pages(
            question=question,
            evaluation_text=evaluation_text,
            tasks=tasks,
            findings=findings,
            call=call,
            db=db,
            trace=trace,
            broadcaster=broadcaster,
        )
        log.info("Stage 3 complete: %d affected pages identified", len(affected))

        await trace.record(
            AffectedPagesIdentifiedEvent(
                affected_pages=[
                    {"page_id": a.page_id, "findings_summary": a.findings_summary}
                    for a in affected
                ],
            )
        )

        if not affected:
            call.result_summary = (
                "No pages directly affected by web research findings."
            )
            call.status = CallStatus.COMPLETE
            await db.save_call(call)
            return call

        log.info("Stage 4: computing update subgraph")
        subgraph = await _compute_update_subgraph(
            affected, question_id, db
        )
        log.info(
            "Stage 4 complete: %d nodes in update subgraph",
            len(subgraph),
        )

        await trace.record(
            UpdateSubgraphComputedEvent(
                node_count=len(subgraph),
                nodes=[
                    {
                        "page_id": n.page_id[:8],
                        "node_type": n.node_type,
                        "input_count": len(n.input_ids),
                        "has_findings": n.findings is not None,
                    }
                    for n in subgraph.values()
                ],
            )
        )

        log.info("Stage 5: executing graph updates")
        await _execute_update_graph(subgraph, call, db, trace)
        log.info("Stage 5 complete")

        log.info("Stage 6: generating abstracts and embeddings")
        await _generate_abstracts(call, db)
        log.info("Stage 6 complete")

        call.result_summary = (
            f"Grounding feedback complete: {len(tasks)} claims investigated, "
            f"{len(findings)} web research results collected, "
            f"{len(affected)} pages directly affected, "
            f"{len(subgraph)} pages updated."
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
        f"grounding issues and lists the relevant workspace page IDs.\n\n"
        f"{briefing}\n"
        f"</briefing>\n\n"
        f"<web_research_findings>\n"
        f"Web research agents investigated the issues outlined in the "
        f"briefing above. Here is what they found.\n\n"
        f"{findings_text}\n"
        f"</web_research_findings>"
    )


async def _identify_affected_pages(
    question: Page,
    evaluation_text: str,
    tasks: Sequence[GroundingTask],
    findings: Sequence[tuple[GroundingTask, str]],
    call: Call,
    db: DB,
    trace: CallTrace,
    broadcaster: Broadcaster | None = None,
) -> Sequence[AffectedPage]:
    """Stage 3: use an agent to identify which pages are directly affected."""
    system_prompt = (
        (_PROMPTS_DIR / "preamble.md").read_text()
        + "\n\n"
        + (_PROMPTS_DIR / "grounding-identify-affected.md").read_text()
    )

    explore_tool = make_explore_tool(db)

    user_prompt = await _build_identification_user_message(
        question, evaluation_text, tasks, findings, call, db
    )

    config = SdkAgentConfig(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        server_name=_TOOL_SERVER_NAME,
        mcp_tools=[explore_tool],
        call=call,
        call_type=CallType.GROUNDING_FEEDBACK,
        scope_page_id=question.id,
        db=db,
        trace=trace,
        broadcaster=broadcaster,
        disallowed_tools=("Write", "Edit", "Bash", "Glob", "Grep", "Read"),
        output_format=AffectedPageList.model_json_schema(),
    )

    result = await run_sdk_agent(config)

    if result.structured_output is None:
        log.warning("Stage 3 agent returned no structured output")
        return []

    affected_list = AffectedPageList.model_validate(result.structured_output)
    return affected_list.affected_pages


async def _compute_update_subgraph(
    affected: Sequence[AffectedPage],
    target_question_id: str,
    db: DB,
) -> dict[str, UpdateNode]:
    """Stage 4: compute the subgraph from affected pages up to the target question.

    Returns a dict mapping page ID -> UpdateNode. Each node's input_ids
    contains the page IDs that must be updated before it.
    """
    findings_by_id: dict[str, str] = {}
    seed_ids: set[str] = set()
    for ap in affected:
        resolved = await db.resolve_page_id(ap.page_id)
        if resolved:
            seed_ids.add(resolved)
            findings_by_id[resolved] = ap.findings_summary
        else:
            log.warning("Could not resolve affected page ID: %s", ap.page_id)

    if not seed_ids:
        return {}

    nodes: dict[str, UpdateNode] = {}
    visited: set[str] = set()

    async def _walk_up(page_id: str) -> None:
        """BFS upward from a page to the target question, collecting nodes."""
        if page_id in visited:
            return
        visited.add(page_id)

        page = await db.get_page(page_id)
        if not page or not page.is_active():
            return

        if page.page_type == PageType.CLAIM:
            if page_id not in nodes:
                nodes[page_id] = UpdateNode(
                    page_id=page_id,
                    page=page,
                    node_type="claim",
                    findings=findings_by_id.get(page_id),
                )
            # Walk up: find questions this claim is a consideration on
            links = await db.get_links_from(page_id)
            for link in links:
                if link.link_type == LinkType.CONSIDERATION:
                    await _walk_up(link.to_page_id)

        elif page.page_type == PageType.QUESTION:
            # Add the question node (judgement will be reassessed)
            if page_id not in nodes:
                nodes[page_id] = UpdateNode(
                    page_id=page_id,
                    page=page,
                    node_type="question",
                    question_id=page_id,
                    findings=findings_by_id.get(page_id),
                )
            # Walk up: find parent questions
            if page_id != target_question_id:
                parent = await db.get_parent_question(page_id)
                if parent:
                    await _walk_up(parent.id)

        elif page.page_type == PageType.JUDGEMENT:
            # Judgements are attached to questions; walk to the question
            links = await db.get_links_from(page_id)
            for link in links:
                if link.link_type == LinkType.RELATED:
                    await _walk_up(link.to_page_id)

    for sid in seed_ids:
        await _walk_up(sid)

    # Build dependency edges: a question node depends on its considerations
    # and child-question nodes that are in the subgraph
    for node in nodes.values():
        if node.node_type == "question":
            qid = node.question_id
            assert qid is not None
            # Considerations (claims) in the subgraph are dependencies
            considerations = await db.get_considerations_for_question(qid)
            for claim_page, _link in considerations:
                if claim_page.id in nodes:
                    node.input_ids.add(claim_page.id)
            # Child questions in the subgraph are dependencies
            children = await db.get_child_questions(qid)
            for child in children:
                if child.id in nodes:
                    node.input_ids.add(child.id)

    _log_subgraph(nodes)
    return nodes


def _log_subgraph(nodes: dict[str, UpdateNode]) -> None:
    """Log the update subgraph for visibility."""
    lines = ["Update subgraph:"]
    for node in nodes.values():
        deps = ", ".join(d[:8] for d in node.input_ids) if node.input_ids else "none"
        findings_marker = " [has findings]" if node.findings else ""
        lines.append(
            f"  {node.page_id[:8]} ({node.node_type}){findings_marker} "
            f"← depends on: {deps}"
        )
    log.info("\n".join(lines))


async def _execute_update_graph(
    nodes: dict[str, UpdateNode],
    call: Call,
    db: DB,
    trace: CallTrace,
) -> None:
    """Stage 5: update all nodes concurrently respecting dependencies."""
    if not nodes:
        return

    completion_events: dict[str, asyncio.Event] = {
        pid: asyncio.Event() for pid in nodes
    }

    async def _update_one(node: UpdateNode) -> None:
        for dep_id in node.input_ids:
            if dep_id in completion_events:
                await completion_events[dep_id].wait()

        async with _UPDATE_SEMAPHORE:
            if node.node_type == "claim":
                await _reassess_claim(node, call, db, trace)
            elif node.node_type == "question":
                await _reassess_judgement(node, call, db, trace)

        completion_events[node.page_id].set()

    await asyncio.gather(*[_update_one(n) for n in nodes.values()])


async def _reassess_claim(
    node: UpdateNode,
    call: Call,
    db: DB,
    trace: CallTrace,
) -> None:
    """Reassess a claim using embedding context + linked pages + findings."""
    old_page = node.page
    system_prompt = (
        (_PROMPTS_DIR / "preamble.md").read_text()
        + "\n\n"
        + (_PROMPTS_DIR / "grounding-reassess-claim.md").read_text()
    )

    # Build embedding-based context around this claim
    ctx_result = await build_embedding_based_context(
        old_page.headline,
        db,
    )

    # Expand linked pages
    links_from = await db.get_links_from(old_page.id)
    links_to = await db.get_links_to(old_page.id)
    linked_ids = (
        {l.to_page_id for l in links_from}
        | {l.from_page_id for l in links_to}
    )
    linked_pages = await db.get_pages_by_ids(list(linked_ids))
    linked_text_parts: list[str] = []
    for pid, lp in linked_pages.items():
        if lp.is_active():
            linked_text_parts.append(
                f"### `{pid[:8]}` — {lp.headline} ({lp.page_type.value})\n\n"
                f"{lp.content}"
            )
    linked_text = "\n\n---\n\n".join(linked_text_parts) if linked_text_parts else ""

    user_parts: list[str] = [
        f"## Workspace context\n\n{ctx_result.context_text}",
    ]
    if linked_text:
        user_parts.append(f"## Linked pages\n\n{linked_text}")
    user_parts.append(
        f"## Claim to reassess\n\n"
        f"**Headline:** {old_page.headline}\n"
        f"**ID:** `{old_page.id[:8]}`\n\n"
        f"{old_page.content}"
    )
    if node.findings:
        user_parts.append(
            f"## Web research findings\n\n"
            f"The following findings directly bear on this claim:\n\n"
            f"{node.findings}"
        )

    user_message = "\n\n".join(user_parts)

    meta = LLMExchangeMetadata(
        call_id=call.id,
        phase=f"reassess_claim_{old_page.id[:8]}",
    )
    result = await structured_call(
        system_prompt,
        user_message=user_message,
        response_model=ReassessedClaim,
        metadata=meta,
        db=db,
    )

    if result.data is None:
        log.warning("Reassess claim %s returned no data", old_page.id[:8])
        return

    reassessed = ReassessedClaim.model_validate(result.data)

    # Create the new claim page
    new_page = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=old_page.workspace,
        content=reassessed.content,
        headline=reassessed.headline,
        epistemic_status=reassessed.epistemic_status,
        epistemic_type=reassessed.epistemic_type,
        provenance_model="claude-opus-4-6",
        provenance_call_type=call.call_type.value,
        provenance_call_id=call.id,
    )
    await db.save_page(new_page)
    write_page_file(new_page)

    # Supersede old claim
    await db.supersede_page(old_page.id, new_page.id)

    # Re-link: copy consideration links from old claim to new claim
    for link in links_from:
        if link.link_type == LinkType.CONSIDERATION:
            new_link = PageLink(
                from_page_id=new_page.id,
                to_page_id=link.to_page_id,
                link_type=LinkType.CONSIDERATION,
                direction=link.direction or ConsiderationDirection.SUPPORTS,
                strength=link.strength,
                reasoning=link.reasoning,
            )
            await db.save_link(new_link)

    log.info(
        "Reassessed claim %s -> %s: %s",
        old_page.id[:8],
        new_page.id[:8],
        reassessed.headline[:70],
    )

    await trace.record(
        ClaimReassessedEvent(
            old_page_id=old_page.id,
            new_page_id=new_page.id,
            headline=reassessed.headline,
        )
    )


async def _reassess_judgement(
    node: UpdateNode,
    call: Call,
    db: DB,
    trace: CallTrace,
) -> None:
    """Reassess a question's judgement by dispatching an AssessCall."""
    qid = node.question_id
    assert qid is not None

    page = node.page
    assess_call = await db.create_call(
        CallType.ASSESS,
        scope_page_id=qid,
        parent_call_id=call.id,
    )
    cls = ASSESS_CALL_CLASSES[get_settings().assess_call_variant]
    assess = cls(qid, assess_call, db)
    await assess.run()

    log.info(
        "Reassessed judgement for question %s (call %s)",
        qid[:8],
        assess_call.id[:8],
    )

    await trace.record(
        ReassessTriggeredEvent(
            question_id=qid,
            question_headline=page.headline,
            child_call_id=assess_call.id,
        )
    )


class _PageAbstractList(BaseModel):
    summaries: list[PageSummaryItem]


async def _generate_abstracts(call: Call, db: DB) -> None:
    """Stage 6: generate abstracts and embeddings for pages created in this call."""
    rows = (
        await db._execute(
            db.client.table("pages")
            .select("id, headline, content, page_type")
            .eq("provenance_call_id", call.id)
            .neq("page_type", "source")
        )
    )
    pages = [r for r in (rows.data or []) if r.get("id")]
    if not pages:
        log.info("Stage 6: no pages to abstract")
        return

    page_lines = "\n".join(
        f'- `{p["id"][:8]}`: "{p["headline"][:120]}"'
        for p in pages
    )
    user_message = (
        "Generate an abstract for each of the following pages.\n\n"
        f"{page_lines}\n\n"
        f"Abstract requirements: {ABSTRACT_INSTRUCTION}\n\n"
        "For each page, return its page_id and abstract."
    )

    page_contents = "\n\n---\n\n".join(
        f'Page `{p["id"][:8]}` — {p["headline"]}\n\n{p["content"]}'
        for p in pages
    )
    system_prompt = (
        "You are generating abstracts for workspace pages. "
        "You will be given page contents and must produce a self-contained "
        "abstract for each.\n\n"
        f"Page contents:\n\n{page_contents}"
    )

    meta = LLMExchangeMetadata(
        call_id=call.id,
        phase="abstract_generation",
    )
    result = await structured_call(
        system_prompt,
        user_message=user_message,
        response_model=_PageAbstractList,
        metadata=meta,
        db=db,
    )
    if result.data:
        parsed = _PageAbstractList(**result.data)
        await save_page_abstracts(parsed.summaries, db)
