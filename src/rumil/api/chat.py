"""Chat endpoint for the worldview UI.

Wraps the Anthropic API with tools that operate on the rumil workspace.
The model sees the research context and can search, inspect, create, and
dispatch — the same capabilities as the CC skills layer, exposed via HTTP.
"""

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from pathlib import Path
from typing import Any

from fastapi.responses import StreamingResponse

import anthropic
from anthropic.types import TextBlock, TextDelta, ToolUseBlock
from pydantic import BaseModel

from rumil.calls import (
    ASSESS_CALL_CLASSES,
    FindConsiderationsCall,
    IngestCall,
    WebResearchCall,
    ScoutAnalogiesCall,
    ScoutEstimatesCall,
    ScoutHypothesesCall,
    ScoutSubquestionsCall,
)
from rumil.calls.stages import CallRunner
from rumil.context import build_embedding_based_context
from rumil.database import DB
from rumil.embeddings import embed_query, search_pages_by_vector
from rumil.models import CallType, MoveType, Page, PageLayer, PageType, Workspace
from rumil.moves.registry import MOVES
from rumil.scraper import scrape_url
from rumil.settings import get_settings
from rumil.summary import build_research_tree
from rumil.views import build_view

log = logging.getLogger(__name__)
PROMPTS_DIR = Path(__file__).parent.parent.parent.parent / "prompts"


MODEL_MAP: dict[str, str] = {
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-6",
    "haiku": "claude-haiku-4-5-20251001",
}


class ChatRequest(BaseModel):
    question_id: str
    messages: list[dict[str, Any]]
    workspace: str = "default"
    model: str = "sonnet"


class ToolUseInfo(BaseModel):
    name: str
    input: dict[str, Any]
    result: str


class ChatResponse(BaseModel):
    response: str
    tool_uses: list[ToolUseInfo]


TOOLS: list[dict[str, Any]] = [
    {
        "name": "search_workspace",
        "description": (
            "Search the research workspace by semantic similarity. "
            "Returns the most relevant pages (claims, questions, judgements, "
            "evidence) matching the query."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural language search query",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_page",
        "description": (
            "Fetch a specific page by its short ID (8 characters). "
            "Returns full content, credence/robustness scores, and linked pages."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "short_id": {
                    "type": "string",
                    "description": "8-character page ID",
                },
            },
            "required": ["short_id"],
        },
    },
    {
        "name": "create_question",
        "description": (
            "Add a new research question to the workspace. "
            "Optionally link it under a parent question."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "headline": {
                    "type": "string",
                    "description": "The question to investigate",
                },
                "content": {
                    "type": "string",
                    "description": "Optional elaboration on the question",
                },
                "parent_id": {
                    "type": "string",
                    "description": (
                        "Short ID of the parent question to link under "
                        "(creates a child_question link)"
                    ),
                },
            },
            "required": ["headline"],
        },
    },
    {
        "name": "list_workspace",
        "description": (
            "Show all root questions in the workspace with page counts "
            "and health stats. Use to get an overview of the research state."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "get_suggestions",
        "description": (
            "View the review queue — pending suggestions from research calls "
            "for re-leveling, tension resolution, duplicate merging, etc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["pending", "accepted", "rejected"],
                    "description": "Filter by status (default: pending)",
                },
            },
        },
    },
    {
        "name": "preview_run",
        "description": (
            "Show a preview of what a research call would see — the question's "
            "health stats, section breakdown, and recommendation for what call "
            "type to run next. Use this before dispatch_call to help the user "
            "decide what to investigate."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "question_id": {
                    "type": "string",
                    "description": (
                        "Short ID of the question to preview. "
                        "Omit to auto-pick the current question."
                    ),
                },
            },
        },
    },
    {
        "name": "dispatch_call",
        "description": (
            "Fire a rumil research call against a question. This runs the "
            "full investigation pipeline (LLM calls, context building, etc.) "
            "and COSTS REAL MONEY. Confirm with the user before calling. "
            "The call runs in the background — results appear in the view. "
            "Available call types: find-considerations, assess, web-research, "
            "scout-subquestions, scout-hypotheses, scout-estimates, scout-analogies."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "question_id": {
                    "type": "string",
                    "description": "Short ID of the question to investigate",
                },
                "call_type": {
                    "type": "string",
                    "enum": [
                        "find-considerations",
                        "assess",
                        "web-research",
                        "scout-subquestions",
                        "scout-hypotheses",
                        "scout-estimates",
                        "scout-analogies",
                    ],
                    "description": "Type of research call to fire",
                },
            },
            "required": ["question_id", "call_type"],
        },
    },
    {
        "name": "ingest_source",
        "description": (
            "Ingest a URL as a source — fetch its content, create a Source page, "
            "and optionally run extraction to pull evidence into a target question. "
            "COSTS REAL MONEY if extraction is requested. Use when the user shares "
            "a URL and wants its findings added to the research."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL to fetch and ingest",
                },
                "target_question_id": {
                    "type": "string",
                    "description": (
                        "Short ID of the question to extract evidence into. "
                        "If omitted, the source is saved but no extraction happens."
                    ),
                },
            },
            "required": ["url"],
        },
    },
    {
        "name": "start_research",
        "description": (
            "Start a sustained research program on a question. Runs multiple "
            "research calls in sequence (find-considerations, assess, scouts) "
            "automatically choosing the right type for each step based on the "
            "question's state. More thorough than a single dispatch_call. "
            "COSTS REAL MONEY proportional to budget. Confirm with the user first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "question_id": {
                    "type": "string",
                    "description": "Short ID of the question to research",
                },
                "budget": {
                    "type": "integer",
                    "description": "Max number of calls to run (default 3, max 10)",
                },
            },
            "required": ["question_id"],
        },
    },
]

_CALL_TYPE_MAP: dict[str, tuple[CallType, type[CallRunner]]] = {
    "find-considerations": (CallType.FIND_CONSIDERATIONS, FindConsiderationsCall),
    "assess": (CallType.ASSESS, ASSESS_CALL_CLASSES["default"]),
    "web-research": (CallType.WEB_RESEARCH, WebResearchCall),
    "scout-subquestions": (CallType.SCOUT_SUBQUESTIONS, ScoutSubquestionsCall),
    "scout-hypotheses": (CallType.SCOUT_HYPOTHESES, ScoutHypothesesCall),
    "scout-estimates": (CallType.SCOUT_ESTIMATES, ScoutEstimatesCall),
    "scout-analogies": (CallType.SCOUT_ANALOGIES, ScoutAnalogiesCall),
}


def _format_page(page: Page) -> str:
    parts = [
        f"[{page.id[:8]}] {page.page_type.value}: {page.headline}",
    ]
    if page.credence is not None:
        parts.append(f"  Credence: {page.credence}/9")
    if page.robustness is not None:
        parts.append(f"  Robustness: {page.robustness}/5")
    if page.content:
        parts.append(f"  {page.content[:500]}")
    return "\n".join(parts)


async def _execute_tool(
    name: str,
    tool_input: dict[str, Any],
    db: DB,
    scope_question_id: str = "",
) -> str:
    """Execute a tool call and return the result as a string."""
    if name == "search_workspace":
        query = tool_input["query"]
        vector = await embed_query(query)
        results = await search_pages_by_vector(
            db, vector, match_count=8, match_threshold=0.3
        )
        if not results:
            return "No matching pages found."
        lines = [f"Found {len(results)} relevant pages:\n"]
        for page, score in results:
            lines.append(_format_page(page))
            lines.append("")
        return "\n".join(lines)

    if name == "get_page":
        short_id = tool_input["short_id"]
        full_id = await db.resolve_page_id(short_id)
        if not full_id:
            return f"No page found matching ID '{short_id}'"
        page = await db.get_page(full_id)
        if not page:
            return f"Page {short_id} not found"
        result = _format_page(page)
        links = await db.get_links_from(full_id)
        if links:
            result += "\n\nOutgoing links:\n"
            for link in links:
                target = await db.get_page(link.to_page_id)
                target_label = (
                    f"{target.id[:8]} ({target.headline})" if target else link.to_page_id[:8]
                )
                result += f"  \u2192 {link.link_type.value}: {target_label}\n"
        return result

    if name == "create_question":
        headline = tool_input["headline"]
        content = tool_input.get("content", "")
        parent_short = tool_input.get("parent_id")

        move_def = MOVES[MoveType.CREATE_QUESTION]
        payload = {"headline": headline}
        if content:
            payload["content"] = content
        validated = move_def.schema(**payload)

        call = await db.create_call(
            CallType.CLAUDE_CODE_DIRECT, scope_page_id=None
        )
        result = await move_def.execute(validated, call, db)

        created_id = result.created_page_id or ""
        response_parts = [f"Created question: {created_id[:8]} ({headline})"]

        if parent_short and created_id:
            parent_full = await db.resolve_page_id(parent_short)
            if parent_full:
                link_def = MOVES[MoveType.LINK_CHILD_QUESTION]
                link_payload = link_def.schema(
                    parent_id=parent_full,
                    child_id=result.created_page_id,
                )
                await link_def.execute(link_payload, call, db)
                response_parts.append(
                    f"Linked as child of {parent_short}"
                )

        return "\n".join(response_parts)

    if name == "list_workspace":
        questions = await db.get_root_questions(Workspace.RESEARCH)
        if not questions:
            return "No root questions in this workspace."
        lines = [f"{len(questions)} root question(s):\n"]
        for q in questions:
            counts = await db.count_pages_for_question(q.id)
            total = counts.get("considerations", 0) + counts.get("judgements", 0)
            lines.append(
                f"  [{q.id[:8]}] {q.headline}"
                f"  ({total} pages)"
            )
        return "\n".join(lines)

    if name == "get_suggestions":
        status = tool_input.get("status", "pending")
        suggestions = await db.get_suggestions(status=status)
        if not suggestions:
            return f"No {status} suggestions."
        page_ids = list({s.target_page_id for s in suggestions if s.target_page_id})
        pages = await db.get_pages_by_ids(page_ids) if page_ids else {}
        lines = [f"{len(suggestions)} {status} suggestion(s):\n"]
        for s in suggestions[:20]:
            target = pages.get(s.target_page_id)
            target_label = target.headline if target else s.target_page_id[:8]
            reasoning = (s.payload.get("reasoning") or "")[:150]
            lines.append(f"  [{s.id[:8]}] {s.suggestion_type.value} \u2192 {target_label}")
            if reasoning:
                lines.append(f"    {reasoning}")
            lines.append("")
        return "\n".join(lines)

    if name == "preview_run":
        qid_short = tool_input.get("question_id") or scope_question_id[:8]
        full_id = await db.resolve_page_id(qid_short)
        if not full_id:
            return f"Question '{qid_short}' not found."
        try:
            view = await build_view(db, full_id)
        except ValueError as e:
            return str(e)
        h = view.health
        section_summary = ", ".join(
            f"{s.name.replace('_', ' ')}({len(s.items)})" for s in view.sections
        )
        recommendation = "find-considerations" if h.total_pages < 5 else "assess"
        if h.child_questions_without_judgements > 2:
            recommendation = "find-considerations"
        lines = [
            f"Preview for: {view.question.headline} [{view.question.id[:8]}]\n",
            f"Pages: {h.total_pages}",
            f"Max depth: {h.max_depth}",
            f"Missing credence: {h.missing_credence}",
            f"Missing importance: {h.missing_importance}",
            f"Child questions without judgements: {h.child_questions_without_judgements}",
            f"\nSections: {section_summary}",
            f"\nRecommended call type: {recommendation}",
        ]
        return "\n".join(lines)

    if name == "dispatch_call":
        qid_short = tool_input["question_id"]
        call_type_str = tool_input["call_type"]
        full_id = await db.resolve_page_id(qid_short)
        if not full_id:
            return f"Question '{qid_short}' not found."
        if call_type_str not in _CALL_TYPE_MAP:
            return f"Unknown call type: {call_type_str}"
        ct, cls = _CALL_TYPE_MAP[call_type_str]
        call = await db.create_call(ct, scope_page_id=full_id)
        runner = cls(full_id, call, db)

        async def _run_call() -> None:
            try:
                await runner.run()
            except Exception:
                log.exception("Background call %s failed", call.id[:8])

        asyncio.create_task(_run_call())
        return (
            f"Dispatched {call_type_str} call {call.id[:8]} on question {qid_short}. "
            f"Running in background \u2014 results will appear in the view when complete."
        )

    if name == "start_research":
        qid_short = tool_input["question_id"]
        budget = max(1, min(tool_input.get("budget", 3), 10))
        full_id = await db.resolve_page_id(qid_short)
        if not full_id:
            return f"Question '{qid_short}' not found."
        question = await db.get_page(full_id)
        if not question:
            return f"Question '{qid_short}' not found."
        return json.dumps({
            "__async_research__": True,
            "question_id": full_id,
            "headline": question.headline,
            "budget": budget,
        })

    if name == "ingest_source":
        url = tool_input["url"]
        target_short = tool_input.get("target_question_id")
        scraped = await scrape_url(url)
        if not scraped:
            return f"Failed to fetch URL: {url}"
        source_page = Page(
            page_type=PageType.SOURCE,
            layer=PageLayer.SQUIDGY,
            workspace=Workspace.RESEARCH,
            headline=scraped.title or url,
            content=scraped.content,
            extra={"url": url},
            project_id=db.project_id,
        )
        await db.save_page(source_page)
        result_parts = [f"Created source page {source_page.id[:8]}: {scraped.title or url}"]
        if target_short:
            full_id = await db.resolve_page_id(target_short)
            if full_id:
                call = await db.create_call(CallType.INGEST, scope_page_id=full_id)
                runner = IngestCall(source_page, full_id, call, db)

                async def _run_ingest() -> None:
                    try:
                        await runner.run()
                    except Exception:
                        log.exception("Ingest call %s failed", call.id[:8])

                asyncio.create_task(_run_ingest())
                result_parts.append(
                    f"Dispatched ingest extraction call {call.id[:8]} "
                    f"targeting question {target_short}."
                )
            else:
                result_parts.append(f"Target question '{target_short}' not found \u2014 source saved but no extraction.")
        return "\n".join(result_parts)

    return f"Unknown tool: {name}"


def _pick_call_type(total_pages: int, missing_credence: int, step: int) -> str:
    """Simple heuristic for what call to run next."""
    if total_pages < 5:
        return "find-considerations"
    if step == 0 and missing_credence > 3:
        return "assess"
    if step % 3 == 0:
        return "find-considerations"
    if step % 3 == 1:
        return "assess"
    return "scout-subquestions"


async def _run_research(
    db: DB,
    params: dict[str, Any],
    on_progress: Callable[[str], Any] | None = None,
) -> str:
    """Run a multi-step research program on a single question."""
    question_id = params["question_id"]
    headline = params.get("headline", question_id[:8])
    budget = params.get("budget", 3)

    if on_progress:
        on_progress(f"Starting {budget}-step research on '{headline[:40]}'...")

    step_summaries: list[str] = []
    for i in range(budget):
        try:
            view = await build_view(db, question_id)
            call_type_str = _pick_call_type(
                view.health.total_pages, view.health.missing_credence, i
            )
        except Exception:
            call_type_str = "find-considerations"

        if on_progress:
            on_progress(f"Step {i + 1}/{budget}: {call_type_str} on '{headline[:30]}'...")

        if call_type_str not in _CALL_TYPE_MAP:
            step_summaries.append(f"  Step {i + 1}: unknown call type {call_type_str}")
            continue

        ct, cls = _CALL_TYPE_MAP[call_type_str]
        call = await db.create_call(ct, scope_page_id=question_id)
        runner = cls(question_id, call, db)
        try:
            await runner.run()
            step_summaries.append(f"  Step {i + 1}: {call_type_str} \u2014 call {call.id[:8]} completed")
            if on_progress:
                on_progress(f"Step {i + 1} done: {call_type_str} call {call.id[:8]}")
        except Exception as e:
            step_summaries.append(f"  Step {i + 1}: {call_type_str} \u2014 error: {e}")
            if on_progress:
                on_progress(f"Step {i + 1} error: {e}")

    return (
        f"Research on '{headline[:40]}' completed ({len(step_summaries)}/{budget} steps):\n"
        + "\n".join(step_summaries)
    )


async def build_chat_context(
    question_id: str,
    db: DB,
) -> str:
    """Build the context string for the chat model."""
    question = await db.get_page(question_id)
    if not question:
        return "Question not found."

    parts: list[str] = []

    parts.append(f"# Current question: {question.headline}\n")
    if question.content:
        parts.append(f"{question.content}\n")

    parts.append("## Research tree\n")
    tree = await build_research_tree(question_id, db, max_depth=3)
    parts.append(tree)
    parts.append("")

    parts.append("## Workspace neighbors (by embedding similarity)\n")
    neighbor_result = await build_embedding_based_context(
        question.content or question.headline,
        db,
        scope_question_id=question_id,
    )
    if neighbor_result.context_text:
        parts.append(neighbor_result.context_text)

    return "\n".join(parts)


async def handle_chat(request: ChatRequest) -> ChatResponse:
    """Handle a chat request: build context, call LLM with tools, return response."""
    settings = get_settings()
    db = await DB.create(
        run_id=str(uuid.uuid4()),
        prod=settings.is_prod_db,
    )
    project = await db.get_or_create_project(request.workspace)
    db.project_id = project.id

    try:
        full_id = await db.resolve_page_id(request.question_id)
        if not full_id:
            return ChatResponse(
                response=f"No question found matching '{request.question_id}'",
                tool_uses=[],
            )

        system_prompt = (PROMPTS_DIR / "api_chat.md").read_text(encoding="utf-8")
        context = await build_chat_context(full_id, db)
        full_system = f"{system_prompt}\n\n---\n\n{context}"

        model_id = MODEL_MAP.get(request.model, MODEL_MAP["sonnet"])
        client = anthropic.AsyncAnthropic(api_key=settings.require_anthropic_key())
        messages = list(request.messages)
        tool_uses_log: list[ToolUseInfo] = []

        for _ in range(10):
            response = await client.messages.create(
                model=model_id,
                max_tokens=4096,
                temperature=0.7,
                system=full_system,
                messages=messages,  # type: ignore[arg-type]
                tools=TOOLS,  # type: ignore[arg-type]
            )

            text_parts: list[str] = []
            tool_calls: list[ToolUseBlock] = []
            for block in response.content:
                if isinstance(block, TextBlock):
                    text_parts.append(block.text)
                elif isinstance(block, ToolUseBlock):
                    tool_calls.append(block)

            if not tool_calls:
                return ChatResponse(
                    response="\n".join(text_parts),
                    tool_uses=tool_uses_log,
                )

            messages.append({"role": "assistant", "content": response.content})

            tool_results = []
            for tc in tool_calls:
                result_str = await _execute_tool(tc.name, tc.input, db, full_id)
                if '"__async_research__"' in result_str:
                    params = json.loads(result_str)
                    result_str = await _run_research(db, params)
                tool_uses_log.append(
                    ToolUseInfo(
                        name=tc.name,
                        input=tc.input,
                        result=result_str[:500],
                    )
                )
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tc.id,
                        "content": result_str,
                    }
                )

            messages.append({"role": "user", "content": tool_results})

        return ChatResponse(
            response="Reached maximum tool-use rounds.",
            tool_uses=tool_uses_log,
        )
    finally:
        await db.close()


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


async def handle_chat_stream(request: ChatRequest) -> StreamingResponse:
    """Handle a streaming chat request, yielding SSE events."""
    settings = get_settings()
    db = await DB.create(
        run_id=str(uuid.uuid4()),
        prod=settings.is_prod_db,
    )
    project = await db.get_or_create_project(request.workspace)
    db.project_id = project.id

    full_id = await db.resolve_page_id(request.question_id)
    if not full_id:
        async def error_gen() -> AsyncIterator[str]:
            yield _sse("error", {"message": f"No question found matching '{request.question_id}'"})
        return StreamingResponse(error_gen(), media_type="text/event-stream")

    system_prompt = (PROMPTS_DIR / "api_chat.md").read_text(encoding="utf-8")
    context = await build_chat_context(full_id, db)
    full_system = f"{system_prompt}\n\n---\n\n{context}"
    model_id = MODEL_MAP.get(request.model, MODEL_MAP["sonnet"])
    client = anthropic.AsyncAnthropic(api_key=settings.require_anthropic_key())
    messages = list(request.messages)

    async def generate() -> AsyncIterator[str]:
        nonlocal messages
        try:
            for _ in range(10):
                async with client.messages.stream(
                    model=model_id,
                    max_tokens=4096,
                    temperature=0.7,
                    system=full_system,
                    messages=messages,  # type: ignore[arg-type]
                    tools=TOOLS,  # type: ignore[arg-type]
                ) as stream:
                    async for event in stream:
                        if event.type == "content_block_delta":
                            if isinstance(event.delta, TextDelta):
                                yield _sse("text", {"content": event.delta.text})
                        elif event.type == "content_block_start":
                            if isinstance(event.content_block, ToolUseBlock):
                                yield _sse("tool_use_start", {"name": event.content_block.name})

                response = await stream.get_final_message()

                tool_calls = [
                    b for b in response.content if isinstance(b, ToolUseBlock)
                ]
                if not tool_calls:
                    break

                messages.append({"role": "assistant", "content": response.content})

                tool_results = []
                for tc in tool_calls:
                    result_str = await _execute_tool(tc.name, tc.input, db, full_id)
                    if '"__async_research__"' in result_str:
                        params = json.loads(result_str)
                        progress_q: asyncio.Queue[str | None] = asyncio.Queue()
                        task = asyncio.create_task(
                            _run_research(db, params, on_progress=lambda m: progress_q.put_nowait(m))
                        )
                        while not task.done():
                            try:
                                msg = await asyncio.wait_for(progress_q.get(), timeout=0.5)
                                yield _sse("orchestrator_progress", {"message": msg})
                            except asyncio.TimeoutError:
                                continue
                        result_str = await task
                        while not progress_q.empty():
                            yield _sse("orchestrator_progress", {"message": progress_q.get_nowait()})
                    yield _sse("tool_use_result", {
                        "name": tc.name,
                        "input": tc.input,
                        "result": result_str[:500],
                    })
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tc.id,
                        "content": result_str,
                    })
                messages.append({"role": "user", "content": tool_results})

            yield _sse("done", {})
        except Exception as e:
            log.error("Chat stream error: %s", e, exc_info=True)
            yield _sse("error", {"message": str(e)})
        finally:
            await db.close()

    return StreamingResponse(generate(), media_type="text/event-stream")
