"""Chat endpoint for the worldview UI.

Wraps the Anthropic API with tools that operate on the rumil workspace.
The model sees the research context and can search, inspect, create, and
dispatch — the same capabilities as the CC skills layer, exposed via HTTP.
"""

import json
import logging
import uuid
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

from fastapi.responses import StreamingResponse

import anthropic
from anthropic.types import TextBlock, TextDelta, ToolUseBlock
from pydantic import BaseModel

from rumil.context import build_embedding_based_context
from rumil.database import DB
from rumil.embeddings import embed_query, search_pages_by_vector
from rumil.models import CallType, MoveType, Page
from rumil.moves.registry import MOVES
from rumil.settings import get_settings
from rumil.summary import build_research_tree

log = logging.getLogger(__name__)
PROMPTS_DIR = Path(__file__).parent.parent.parent.parent / "prompts"


class ChatRequest(BaseModel):
    question_id: str
    messages: list[dict[str, Any]]
    workspace: str = "default"


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
        "name": "dispatch_call",
        "description": (
            "Fire a rumil research call against a question. This runs the "
            "full investigation pipeline (LLM calls, context building, etc.) "
            "and costs real money. Available call types: find-considerations, "
            "assess, web-research, scout-subquestions, scout-hypotheses, "
            "scout-estimates, scout-analogies."
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
]


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
                result += f"  → {link.link_type.value}: {target_label}\n"
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

    if name == "dispatch_call":
        return (
            f"[dispatch_call is not yet wired in the API. "
            f"Requested: {tool_input['call_type']} on {tool_input['question_id']}. "
            f"Use the CLI for now: "
            f"PYTHONPATH=.claude/lib uv run python -m rumil_skills.dispatch_call "
            f"{tool_input['call_type']} {tool_input['question_id']}]"
        )

    return f"Unknown tool: {name}"


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

        client = anthropic.AsyncAnthropic(api_key=settings.require_anthropic_key())
        messages = list(request.messages)
        tool_uses_log: list[ToolUseInfo] = []

        for _ in range(10):
            response = await client.messages.create(
                model=settings.model,
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
                result_str = await _execute_tool(tc.name, tc.input, db)
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
    client = anthropic.AsyncAnthropic(api_key=settings.require_anthropic_key())
    messages = list(request.messages)

    async def generate() -> AsyncIterator[str]:
        nonlocal messages
        try:
            for _ in range(10):
                async with client.messages.stream(
                    model=settings.model,
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
                    result_str = await _execute_tool(tc.name, tc.input, db)
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
