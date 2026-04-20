"""Chat endpoint for the research UI.

Wraps the Anthropic API with tools that operate on the rumil workspace.
The model sees the research context and can search, inspect, create, and
dispatch fire-and-forget research calls.

Fire-and-forget dispatch:
  Dispatching tools (``dispatch_call``, ``ingest_source``) spawn a
  detached asyncio task and return a receipt string immediately. On
  completion the task writes a ``dispatch_result`` chat message onto the
  conversation and publishes a ``dispatch_completed`` event on the
  per-conversation SSE channel. The per-conversation in-memory registry
  is backed by :meth:`DB.get_active_chat_dispatches` so live-dispatch
  visibility survives an API restart.
"""

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anthropic
from anthropic.types import TextBlock, TextDelta, ToolUseBlock
from fastapi.responses import StreamingResponse

from rumil.api.schemas import (
    ChatRequest,
    ChatResponse,
    ToolUseInfo,
)
from rumil.context import build_embedding_based_context
from rumil.database import DB
from rumil.embeddings import embed_query, search_pages_by_vector
from rumil.models import (
    Call,
    CallStatus,
    CallType,
    ChatConversation,
    ChatMessage,
    ChatMessageRole,
    LinkRole,
    LinkType,
    Page,
    PageLayer,
    PageLink,
    PageType,
    Workspace,
)
from rumil.settings import get_settings

log = logging.getLogger(__name__)
PROMPTS_DIR = Path(__file__).parent.parent.parent.parent / "prompts"

MODEL_MAP: dict[str, str] = {
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-7",
    "haiku": "claude-haiku-4-5-20251001",
}

CHAT_TURN_TIMEOUT_S = 600.0

CHAT_RUN_BUDGET = 50
"""Budget initialized for each chat turn's run.

Research calls dispatched from chat consume one unit per agent-loop round
via ``consume_budget``. Without an initialized budget, every round fails
its gate. 50 is plenty of headroom for several multi-round dispatches per
chat turn.
"""

DEFAULT_DISPATCH_MAX_ROUNDS = 4

_live_chat_turns: set[asyncio.Task[Any]] = set()
_live_dispatch_tasks: set[asyncio.Task[Any]] = set()

_live_runs_by_conv: dict[str, dict[str, dict[str, Any]]] = {}
"""Per-conversation registry of in-flight dispatches.

Keyed by conversation_id -> run_id -> metadata. Entries added when a bg
handler spawns, removed when it writes the dispatch_result row. Paired
with :meth:`DB.get_active_chat_dispatches` as a DB-backed fallback.
"""

_conv_event_subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = {}
"""Per-conversation SSE subscriber queues for dispatch_completed events."""


def _register_live_run(
    conv_id: str,
    run_id: str,
    *,
    call_type: str,
    headline: str,
    tool_use_id: str,
    question_id: str | None = None,
) -> None:
    _live_runs_by_conv.setdefault(conv_id, {})[run_id] = {
        "run_id": run_id,
        "call_type": call_type,
        "headline": headline,
        "tool_use_id": tool_use_id,
        "question_id": question_id,
        "started_at": datetime.now(UTC).isoformat(),
    }


def _deregister_live_run(conv_id: str, run_id: str) -> None:
    runs = _live_runs_by_conv.get(conv_id)
    if not runs:
        return
    runs.pop(run_id, None)
    if not runs:
        _live_runs_by_conv.pop(conv_id, None)


def _subscribe_conv_events(conv_id: str) -> asyncio.Queue[dict[str, Any]]:
    q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    _conv_event_subscribers.setdefault(conv_id, set()).add(q)
    return q


def _unsubscribe_conv_events(conv_id: str, q: asyncio.Queue[dict[str, Any]]) -> None:
    subs = _conv_event_subscribers.get(conv_id)
    if not subs:
        return
    subs.discard(q)
    if not subs:
        _conv_event_subscribers.pop(conv_id, None)


def _publish_conv_event(conv_id: str, event: str, data: dict[str, Any]) -> None:
    subs = _conv_event_subscribers.get(conv_id)
    if not subs:
        return
    payload = {"event": event, "data": data}
    for q in list(subs):
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            log.warning("Dropped %s event for conv %s (queue full)", event, conv_id[:8])


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _derive_title(first_user_message: str) -> str:
    text = first_user_message.strip().replace("\n", " ")
    if len(text) > 80:
        text = text[:77].rstrip() + "..."
    return text


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            else:
                text_attr = getattr(block, "text", None)
                if text_attr is not None:
                    parts.append(str(text_attr))
        return "\n".join(p for p in parts if p)
    return ""


def _serialize_assistant_content(content: Sequence[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for block in content:
        if isinstance(block, TextBlock):
            out.append({"type": "text", "text": block.text})
        elif isinstance(block, ToolUseBlock):
            out.append(
                {
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                }
            )
        elif isinstance(block, dict):
            out.append(block)
    return out


def _format_page(page: Page) -> str:
    lines = [f"[{page.id[:8]}] {page.page_type.value}: {page.headline}"]
    if page.abstract:
        lines.append(f"  abstract: {page.abstract[:200]}")
    if page.credence is not None:
        lines.append(f"  credence: {page.credence}")
    return "\n".join(lines)


async def _ensure_conversation(
    db: DB,
    request: ChatRequest,
    question_full_id: str | None,
) -> ChatConversation:
    """Load an existing conversation or auto-create one from the first user message."""
    if request.conversation_id:
        existing = await db.get_chat_conversation(request.conversation_id)
        if existing:
            return existing

    first_user = next(
        (m for m in request.messages if m.get("role") == "user"),
        None,
    )
    first_content = ""
    if first_user:
        content = first_user.get("content")
        if isinstance(content, str):
            first_content = content
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    first_content = str(block.get("text", ""))
                    break
    title = _derive_title(first_content) if first_content else "(new conversation)"
    return await db.create_chat_conversation(
        project_id=db.project_id,
        question_id=question_full_id,
        title=title,
    )


async def _persist_user_turn(
    db: DB,
    conv: ChatConversation,
    request: ChatRequest,
    question_full_id: str | None,
) -> None:
    """Persist every user message from *request* onto the conversation.

    The contract with the frontend is "send only the new user turn(s) for
    this request"; the full history is rebuilt on the server from the
    persisted chat_messages rows. Nothing to dedupe here.
    """
    for m in request.messages:
        if m.get("role") != "user":
            continue
        text = _content_to_text(m.get("content"))
        await db.save_chat_message(
            conversation_id=conv.id,
            role=ChatMessageRole.USER,
            content={"text": text},
            question_id=question_full_id,
        )


def _replay_messages_for_api(
    prior_messages: Sequence[ChatMessage],
) -> list[dict[str, Any]]:
    """Turn persisted chat_messages into Anthropic-API-shaped message dicts."""
    out: list[dict[str, Any]] = []
    for msg in prior_messages:
        if msg.role == ChatMessageRole.USER:
            text = msg.content.get("text", "")
            out.append({"role": "user", "content": text})
        elif msg.role == ChatMessageRole.ASSISTANT:
            blocks = msg.content.get("blocks") or []
            if blocks:
                out.append({"role": "assistant", "content": blocks})
            else:
                text = msg.content.get("text", "")
                if text:
                    out.append({"role": "assistant", "content": text})
        elif msg.role == ChatMessageRole.TOOL_RESULT:
            out.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": msg.content["tool_use_id"],
                            "content": msg.content.get("result", ""),
                        }
                    ],
                }
            )
        elif msg.role == ChatMessageRole.DISPATCH_RESULT:
            run_id = msg.content.get("run_id") or ""
            status = msg.content.get("status") or "unknown"
            summary = msg.content.get("summary") or ""
            call_type = msg.content.get("call_type") or ""
            headline = (msg.content.get("headline") or "").strip()
            hl = f" on '{headline[:60]}'" if headline else ""
            out.append(
                {
                    "role": "user",
                    "content": (
                        f"[dispatch update] run {run_id[:8]} ({call_type}{hl}) {status}: {summary}"
                    ),
                }
            )
    return out


async def build_chat_context(page_id: str, db: DB) -> str:
    """Render a system-prompt context block scoped to the viewed page.

    The `page_id` may point to any page type — question, claim, judgement,
    source, etc. The block leads with the page's own identity and content,
    then adds page-type-specific details (parent chain for questions,
    credence badges for claims/judgements), recent calls scoped to the
    page, and finally an embedding-based neighbour list. Intent: by the
    time the model sees the first user turn, it already knows what the
    user is looking at and what's been tried on it.
    """
    page = await db.get_page(page_id)
    if not page:
        return "Page not found."

    parts: list[str] = [
        f"# {page.page_type.value}: {page.headline}",
        f"_id: {page.id[:8]}_",
    ]

    badges: list[str] = []
    if page.credence is not None:
        badges.append(f"credence={page.credence}")
    if page.robustness is not None:
        badges.append(f"robustness={page.robustness}")
    if page.epistemic_type:
        badges.append(f"epistemic={page.epistemic_type}")
    if badges:
        parts.append(f"_{' · '.join(badges)}_")

    if page.abstract:
        parts.append(f"\n**Abstract**\n{page.abstract}")
    if page.content:
        content_str = page.content
        if len(content_str) > 3000:
            content_str = content_str[:3000] + f"\n… ({len(page.content) - 3000} chars truncated)"
        parts.append(f"\n**Content**\n{content_str}")

    if page.page_type == PageType.QUESTION:
        parent = await db.get_parent_question(page_id)
        if parent:
            parts.append(f"\n**Parent question** · [{parent.id[:8]}] {parent.headline}")
        children = await db.get_child_questions(page_id)
        if children:
            lines = [f"\n**Child questions** ({len(children)})"]
            for c in children[:10]:
                lines.append(f"- [{c.id[:8]}] {c.headline}")
            if len(children) > 10:
                lines.append(f"- … and {len(children) - 10} more")
            parts.append("\n".join(lines))

    call_rows = (
        await db._execute(
            db.client.table("calls")
            .select("id, call_type, status, cost_usd, created_at, result_summary")
            .eq("scope_page_id", page_id)
            .eq("project_id", db.project_id)
            .order("created_at", desc=True)
            .limit(10)
        )
    ).data or []
    if call_rows:
        lines = [f"\n**Recent calls on this page** ({len(call_rows)})"]
        for r in call_rows:
            cid = (r.get("id") or "")[:8]
            ct = r.get("call_type") or "?"
            status = r.get("status") or "?"
            cost = r.get("cost_usd")
            cost_str = f" ${cost:.3f}" if cost else ""
            created = (r.get("created_at") or "")[:10]
            summary = (r.get("result_summary") or "").strip().replace("\n", " ")
            if len(summary) > 120:
                summary = summary[:117] + "…"
            summary_str = f" — {summary}" if summary else ""
            lines.append(f"- [{cid}] {ct} {status}{cost_str} ({created}){summary_str}")
        parts.append("\n".join(lines))

    neighbor_scope = page_id if page.page_type == PageType.QUESTION else None
    neighbor_result = await build_embedding_based_context(
        page.content or page.abstract or page.headline,
        db,
        scope_question_id=neighbor_scope,
    )
    if neighbor_result.context_text:
        parts.append("\n**Workspace neighbors (embedding similarity)**")
        parts.append(neighbor_result.context_text)

    return "\n".join(parts)


TOOLS: list[dict[str, Any]] = [
    {
        "name": "search_workspace",
        "description": (
            "Semantic search over all pages in the active workspace. Returns "
            "the top matches with their short IDs, headlines, and page types."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query text."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_page",
        "description": (
            "Fetch the full content of a single page by 8-char short ID or full "
            "UUID. Returns headline, abstract, content, and outgoing links."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "short_id": {"type": "string", "description": "8-char short ID or full UUID."},
            },
            "required": ["short_id"],
        },
    },
    {
        "name": "list_workspace",
        "description": "List all root (top-level) questions in the active workspace.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_considerations",
        "description": (
            "List considerations (claims, judgements, etc.) that bear on a "
            "specific question. Use this to explore what's been found for a "
            "question."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "question_id": {
                    "type": "string",
                    "description": "Question short ID or full UUID.",
                },
            },
            "required": ["question_id"],
        },
    },
    {
        "name": "get_child_questions",
        "description": "List sub-questions linked as children of a given question.",
        "input_schema": {
            "type": "object",
            "properties": {
                "question_id": {
                    "type": "string",
                    "description": "Parent question short ID or full UUID.",
                },
            },
            "required": ["question_id"],
        },
    },
    {
        "name": "get_incoming_links",
        "description": (
            "List incoming links to a page (which pages point to this one, and what role)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "short_id": {"type": "string", "description": "Page short ID or full UUID."},
            },
            "required": ["short_id"],
        },
    },
    {
        "name": "get_parent_chain",
        "description": (
            "Walk up the parent chain from a question to the root, returning each ancestor."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "question_id": {"type": "string", "description": "Question short ID or full UUID."},
            },
            "required": ["question_id"],
        },
    },
    {
        "name": "get_recent_activity",
        "description": (
            "List the N most recent calls in this project, most recent "
            "first. If `page_id` is provided, only calls whose scope is "
            "that page are returned (use this to see what's been tried on "
            "a specific question/claim)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Default 20.", "default": 20},
                "page_id": {
                    "type": "string",
                    "description": (
                        "Optional page short ID or full UUID. When set, "
                        "filters to calls scoped to this page only."
                    ),
                },
            },
        },
    },
    {
        "name": "navigate_url",
        "description": (
            "Navigate the user's browser to a rumil URL. Use sparingly — "
            "this changes what the user is looking at. Prefer suggest_view "
            "unless the user explicitly asked to be taken somewhere. Paths "
            "must start with / (e.g. '/pages/abc12345', '/traces/...')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path beginning with /.",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "suggest_view",
        "description": (
            "Propose a clickable link for the user. Renders as a chip in "
            "chat; the user decides whether to open it. Use liberally when "
            "referring to pages, traces, or other workspace views."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path beginning with /.",
                },
                "label": {
                    "type": "string",
                    "description": "Short human label (e.g. page headline).",
                },
            },
            "required": ["path", "label"],
        },
    },
    {
        "name": "create_question",
        "description": (
            "Create a new question page. Optionally links as a child of an "
            "existing parent question."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "headline": {"type": "string", "description": "Short question headline."},
                "content": {
                    "type": "string",
                    "description": "Optional body text (can be empty).",
                },
                "parent_id": {
                    "type": "string",
                    "description": "Optional parent question short ID to link as a child.",
                },
            },
            "required": ["headline"],
        },
    },
    {
        "name": "dispatch_call",
        "description": (
            "Fire-and-forget: start a research call on a question. Returns a "
            "receipt immediately; the call runs in the background and a "
            "completion note appears in chat when it finishes. Supported call "
            "types: find_considerations, assess, web_research."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "call_type": {
                    "type": "string",
                    "enum": ["find_considerations", "assess", "web_research"],
                },
                "question_id": {"type": "string", "description": "Question short ID or full UUID."},
                "max_rounds": {
                    "type": "integer",
                    "description": f"Default {DEFAULT_DISPATCH_MAX_ROUNDS}.",
                    "default": DEFAULT_DISPATCH_MAX_ROUNDS,
                },
            },
            "required": ["call_type", "question_id"],
        },
    },
]


async def _handle_search_workspace(db: DB, tool_input: dict[str, Any]) -> str:
    query = tool_input["query"]
    vector = await embed_query(query)
    results = await search_pages_by_vector(db, vector, match_count=8, match_threshold=0.3)
    if not results:
        return "No matching pages found."
    lines = [f"Found {len(results)} relevant pages:\n"]
    for page, _score in results:
        lines.append(_format_page(page))
        lines.append("")
    return "\n".join(lines)


async def _handle_get_page(db: DB, tool_input: dict[str, Any]) -> str:
    short_id = tool_input["short_id"]
    full_id = await db.resolve_page_id(short_id)
    if not full_id:
        return f"No page found matching ID '{short_id}'"
    page = await db.get_page(full_id)
    if not page:
        return f"Page {short_id} not found"
    result = _format_page(page)
    if page.content:
        result += f"\n\nContent:\n{page.content[:2000]}"
    links = await db.get_links_from(full_id)
    if links:
        result += "\n\nOutgoing links:\n"
        for link in links:
            target = await db.get_page(link.to_page_id)
            target_label = f"{target.id[:8]} ({target.headline})" if target else link.to_page_id[:8]
            result += f"  -> {link.link_type.value}: {target_label}\n"
    return result


async def _handle_list_workspace(db: DB, _tool_input: dict[str, Any]) -> str:
    questions = await db.get_root_questions(Workspace.RESEARCH)
    if not questions:
        return "No root questions in this workspace."
    lines = [f"{len(questions)} root question(s):\n"]
    for q in questions:
        counts = await db.count_pages_for_question(q.id)
        total = counts.get("considerations", 0) + counts.get("judgements", 0)
        lines.append(f"  [{q.id[:8]}] {q.headline}  ({total} pages)")
    return "\n".join(lines)


async def _handle_get_considerations(db: DB, tool_input: dict[str, Any]) -> str:
    qid_short = tool_input["question_id"]
    full_id = await db.resolve_page_id(qid_short)
    if not full_id:
        return f"No question found matching '{qid_short}'"
    considerations = await db.get_considerations_for_question(full_id)
    if not considerations:
        return f"No considerations found for question {qid_short}"
    lines = [f"{len(considerations)} consideration(s) for {qid_short}:\n"]
    for page, link in considerations[:30]:
        lines.append(_format_page(page))
        if link.reasoning:
            lines.append(f"  reasoning: {link.reasoning[:200]}")
        lines.append("")
    return "\n".join(lines)


async def _handle_get_child_questions(db: DB, tool_input: dict[str, Any]) -> str:
    qid_short = tool_input["question_id"]
    full_id = await db.resolve_page_id(qid_short)
    if not full_id:
        return f"No question found matching '{qid_short}'"
    children = await db.get_child_questions(full_id)
    if not children:
        return f"No child questions for {qid_short}"
    lines = [f"{len(children)} child question(s) of {qid_short}:\n"]
    for c in children:
        lines.append(f"  [{c.id[:8]}] {c.headline}")
    return "\n".join(lines)


async def _handle_get_incoming_links(db: DB, tool_input: dict[str, Any]) -> str:
    short_id = tool_input["short_id"]
    full_id = await db.resolve_page_id(short_id)
    if not full_id:
        return f"No page found matching '{short_id}'"
    links = await db.get_links_to(full_id)
    if not links:
        return f"No incoming links to {short_id}"
    lines = [f"{len(links)} incoming link(s) to {short_id}:\n"]
    for link in links:
        source = await db.get_page(link.from_page_id)
        source_label = f"{source.id[:8]} ({source.headline})" if source else link.from_page_id[:8]
        lines.append(f"  {link.link_type.value} <- {source_label}")
    return "\n".join(lines)


async def _handle_get_parent_chain(db: DB, tool_input: dict[str, Any]) -> str:
    qid_short = tool_input["question_id"]
    full_id = await db.resolve_page_id(qid_short)
    if not full_id:
        return f"No question found matching '{qid_short}'"
    chain: list[Page] = []
    current: str | None = full_id
    visited: set[str] = set()
    while current and current not in visited:
        visited.add(current)
        parent = await db.get_parent_question(current)
        if not parent:
            break
        chain.append(parent)
        current = parent.id
    if not chain:
        return f"{qid_short} has no parent (it's a root question)"
    lines = [f"Parent chain for {qid_short}:\n"]
    for p in chain:
        lines.append(f"  [{p.id[:8]}] {p.headline}")
    return "\n".join(lines)


async def _handle_get_recent_activity(db: DB, tool_input: dict[str, Any]) -> str:
    limit = int(tool_input.get("limit", 20))
    page_id_in = tool_input.get("page_id")
    scope_full: str | None = None
    if isinstance(page_id_in, str) and page_id_in:
        scope_full = await db.resolve_page_id(page_id_in)
        if not scope_full:
            return f"No page found matching '{page_id_in}'"

    query = (
        db.client.table("calls")
        .select("id, call_type, status, scope_page_id, created_at, cost_usd, result_summary")
        .eq("project_id", db.project_id)
        .order("created_at", desc=True)
        .limit(limit)
    )
    if scope_full:
        query = query.eq("scope_page_id", scope_full)
    rows = (await db._execute(query)).data or []
    if not rows:
        return f"No calls scoped to page {page_id_in}." if scope_full else "No recent calls."
    header = (
        f"{len(rows)} call(s) scoped to {page_id_in}:"
        if scope_full
        else f"{len(rows)} most recent call(s):"
    )
    lines = [header, ""]
    for row in rows:
        scope = row.get("scope_page_id")
        scope_label = "?"
        if scope and not scope_full:
            page = await db.get_page(scope)
            scope_label = page.headline[:60] if page else scope[:8]
        cost = row.get("cost_usd")
        cost_str = f" ${cost:.3f}" if cost else ""
        summary = (row.get("result_summary") or "").strip().replace("\n", " ")
        if len(summary) > 100:
            summary = summary[:97] + "…"
        summary_str = f" — {summary}" if summary else ""
        if scope_full:
            lines.append(
                f"  [{row['id'][:8]}] {row['call_type']} ({row['status']}){cost_str}{summary_str}"
            )
        else:
            lines.append(
                f"  [{row['id'][:8]}] {row['call_type']} ({row['status']}){cost_str} — {scope_label}{summary_str}"
            )
    return "\n".join(lines)


async def _handle_create_question(db: DB, tool_input: dict[str, Any]) -> str:
    headline = tool_input["headline"]
    content = tool_input.get("content", "")
    parent_short = tool_input.get("parent_id")

    page = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        headline=headline,
        content=content,
        project_id=db.project_id,
    )
    await db.save_page(page)
    response_parts = [f"Created question: {page.id[:8]} ({headline})"]

    if parent_short:
        parent_full = await db.resolve_page_id(parent_short)
        if parent_full:
            link = PageLink(
                from_page_id=parent_full,
                to_page_id=page.id,
                link_type=LinkType.CHILD_QUESTION,
                reasoning="Created via chat",
                role=LinkRole.DIRECT,
            )
            await db.save_link(link)
            response_parts.append(f"Linked as child of {parent_short}")
        else:
            response_parts.append(f"(warning: parent {parent_short} not found; skipped link)")

    return "\n".join(response_parts)


def _handle_navigate_url(tool_input: dict[str, Any]) -> str:
    """Sentinel result parsed by the frontend to trigger router.push."""
    path = tool_input.get("path", "").strip()
    if not path.startswith("/"):
        return f"refused: path must start with / (got '{path}')"
    return f"[NAVIGATE]{path}"


def _handle_suggest_view(tool_input: dict[str, Any]) -> str:
    """Sentinel result parsed by the frontend to render a clickable chip."""
    path = tool_input.get("path", "").strip()
    label = tool_input.get("label", "").strip() or path
    if not path.startswith("/"):
        return f"refused: path must start with / (got '{path}')"
    return f"[SUGGEST]{path}|{label}"


_CHAT_DISPATCH_CALL_TYPES: dict[str, CallType] = {
    "find_considerations": CallType.FIND_CONSIDERATIONS,
    "assess": CallType.ASSESS,
    "web_research": CallType.WEB_RESEARCH,
}


async def _run_one_chat_dispatch(
    bg_db: DB,
    call_type: CallType,
    question_id: str,
    max_rounds: int,
) -> Call:
    """Run one call against *bg_db* and return the created Call row.

    Imports the concrete CallRunner class lazily to keep startup light
    and to avoid pulling the full call module graph into the chat hot path.
    """
    call = await bg_db.create_call(call_type, scope_page_id=question_id)
    if call_type == CallType.FIND_CONSIDERATIONS:
        from rumil.calls.find_considerations import FindConsiderationsCall
        from rumil.models import FindConsiderationsMode

        runner = FindConsiderationsCall(
            question_id,
            call,
            bg_db,
            max_rounds=max_rounds,
            fruit_threshold=4,
            mode=FindConsiderationsMode.ALTERNATE,
        )
        await runner.run()
    elif call_type == CallType.ASSESS:
        from rumil.calls.call_registry import ASSESS_CALL_CLASSES

        cls = ASSESS_CALL_CLASSES[get_settings().assess_call_variant]
        assessor = cls(question_id, call, bg_db)
        await assessor.run()
    elif call_type == CallType.WEB_RESEARCH:
        from rumil.calls.web_research import WebResearchCall

        web_runner = WebResearchCall(question_id, call, bg_db)
        await web_runner.run()
    else:
        raise ValueError(f"Unsupported chat-dispatch call_type: {call_type}")
    return call


async def _persist_dispatch_completion(
    bg_db: DB,
    *,
    conv_id: str,
    content: dict[str, Any],
    question_id: str | None,
) -> None:
    try:
        await bg_db.save_chat_message(
            conversation_id=conv_id,
            role=ChatMessageRole.DISPATCH_RESULT,
            content=content,
            question_id=question_id,
        )
        await bg_db.update_chat_conversation(conv_id, touch=True)
    except Exception:
        log.exception(
            "Failed to persist dispatch completion for run %s conv %s",
            (content.get("run_id") or "")[:8],
            conv_id[:8],
        )
    run_id = content.get("run_id")
    if run_id:
        _deregister_live_run(conv_id, run_id)
    _publish_conv_event(conv_id, "dispatch_completed", content)


async def _bg_run_dispatch(
    *,
    conv_id: str,
    tool_use_id: str,
    new_run_id: str,
    project_id: str,
    question_id: str,
    headline: str,
    call_type_str: str,
    max_rounds: int,
) -> None:
    """Background task: run one dispatch call and persist completion."""
    call_type = _CHAT_DISPATCH_CALL_TYPES.get(call_type_str)
    if call_type is None:
        log.error("Background dispatch got unknown call_type %r", call_type_str)
        return

    settings = get_settings()
    bg_db = await DB.create(
        run_id=new_run_id,
        prod=settings.is_prod_db,
        project_id=project_id,
    )
    await bg_db.init_budget(CHAT_RUN_BUDGET)
    trace_url = f"/traces/{new_run_id}"
    content: dict[str, Any]
    try:
        try:
            call = await _run_one_chat_dispatch(bg_db, call_type, question_id, max_rounds)
            content = {
                "tool_use_id": tool_use_id,
                "run_id": new_run_id,
                "call_id": call.id,
                "call_type": call_type_str,
                "question_id": question_id,
                "headline": headline,
                "status": "completed",
                "summary": (f"{call_type_str} call {call.id[:8]} on '{headline[:40]}' completed."),
                "trace_url": trace_url,
            }
        except Exception as e:
            log.exception("Background dispatch %s failed (run %s)", call_type_str, new_run_id[:8])
            content = {
                "tool_use_id": tool_use_id,
                "run_id": new_run_id,
                "call_type": call_type_str,
                "question_id": question_id,
                "headline": headline,
                "status": "failed",
                "summary": f"{call_type_str} call failed: {e}",
                "error": str(e),
                "trace_url": trace_url,
            }

        await _persist_dispatch_completion(
            bg_db,
            conv_id=conv_id,
            content=content,
            question_id=question_id,
        )
    finally:
        await bg_db.close()


def _spawn_bg(coro: Any) -> None:
    task = asyncio.create_task(coro)
    _live_dispatch_tasks.add(task)
    task.add_done_callback(_live_dispatch_tasks.discard)


async def _handle_dispatch_call(
    db: DB,
    tool_input: dict[str, Any],
    *,
    conv_id: str,
    tool_use_id: str,
) -> str:
    call_type_str = tool_input["call_type"]
    qid_short = tool_input["question_id"]
    max_rounds = int(tool_input.get("max_rounds", DEFAULT_DISPATCH_MAX_ROUNDS))

    if call_type_str not in _CHAT_DISPATCH_CALL_TYPES:
        return (
            f"Unsupported call type '{call_type_str}'. "
            f"Supported: {sorted(_CHAT_DISPATCH_CALL_TYPES)}"
        )

    question_id = await db.resolve_page_id(qid_short)
    if not question_id:
        return f"No question found matching '{qid_short}'"
    question = await db.get_page(question_id)
    headline = question.headline if question else qid_short

    new_run_id = str(uuid.uuid4())
    trace_url = f"/traces/{new_run_id}"
    await db.precreate_chat_run_row(
        new_run_id=new_run_id,
        name=f"chat dispatch: {call_type_str} on {headline[:60]}",
        question_id=question_id,
        conv_id=conv_id,
        tool_use_id=tool_use_id,
        call_type=call_type_str,
        headline=headline,
    )
    _register_live_run(
        conv_id,
        new_run_id,
        call_type=call_type_str,
        headline=headline,
        tool_use_id=tool_use_id,
        question_id=question_id,
    )
    _spawn_bg(
        _bg_run_dispatch(
            conv_id=conv_id,
            tool_use_id=tool_use_id,
            new_run_id=new_run_id,
            project_id=db.project_id,
            question_id=question_id,
            headline=headline,
            call_type_str=call_type_str,
            max_rounds=max_rounds,
        )
    )
    return (
        f"{call_type_str} call on '{headline[:40]}' started in background "
        f"(run_id={new_run_id[:8]}). Trace: {trace_url}. A completion note "
        f"will appear in chat once the call finishes — you will not see "
        f"the result in this turn."
    )


async def _handle_tool(
    db: DB,
    name: str,
    tool_input: dict[str, Any],
    *,
    conv_id: str,
    tool_use_id: str,
) -> str:
    if name == "search_workspace":
        return await _handle_search_workspace(db, tool_input)
    if name == "get_page":
        return await _handle_get_page(db, tool_input)
    if name == "list_workspace":
        return await _handle_list_workspace(db, tool_input)
    if name == "get_considerations":
        return await _handle_get_considerations(db, tool_input)
    if name == "get_child_questions":
        return await _handle_get_child_questions(db, tool_input)
    if name == "get_incoming_links":
        return await _handle_get_incoming_links(db, tool_input)
    if name == "get_parent_chain":
        return await _handle_get_parent_chain(db, tool_input)
    if name == "get_recent_activity":
        return await _handle_get_recent_activity(db, tool_input)
    if name == "create_question":
        return await _handle_create_question(db, tool_input)
    if name == "navigate_url":
        return _handle_navigate_url(tool_input)
    if name == "suggest_view":
        return _handle_suggest_view(tool_input)
    if name == "dispatch_call":
        return await _handle_dispatch_call(db, tool_input, conv_id=conv_id, tool_use_id=tool_use_id)
    return f"Unknown tool: {name}"


async def handle_chat(request: ChatRequest) -> ChatResponse:
    """Handle a single chat turn (non-streaming)."""
    settings = get_settings()
    model_id = MODEL_MAP.get(request.model, MODEL_MAP["sonnet"])
    db = await DB.create(run_id=str(uuid.uuid4()), prod=settings.is_prod_db)
    project = await db.get_or_create_project(request.workspace)
    db.project_id = project.id
    await db.create_run(
        name="chat",
        question_id=None,
        config={
            **settings.capture_config(),
            "origin": "chat",
            "model": model_id,
            "chat_model_short": request.model,
        },
    )
    await db.init_budget(CHAT_RUN_BUDGET)
    log.info("chat-turn run=%s ws=%s qid=%s", db.run_id, request.workspace, request.question_id)

    async def run_turn() -> ChatResponse:
        full_id = await db.resolve_page_id(request.question_id) if request.question_id else None
        conv = await _ensure_conversation(db, request, full_id)

        if request.question_id and not full_id:
            return ChatResponse(
                response=f"No question found matching '{request.question_id}'",
                tool_uses=[],
                conversation_id=conv.id,
            )

        prior_messages = await db.list_chat_messages(conv.id)
        replay = _replay_messages_for_api(prior_messages) if prior_messages else []
        messages: list[dict[str, Any]] = replay + list(request.messages)

        await _persist_user_turn(db, conv, request, full_id)

        system_prompt = (PROMPTS_DIR / "api_chat.md").read_text(encoding="utf-8")
        context_text = await build_chat_context(full_id, db) if full_id else "(no question scope)"
        full_system = f"{system_prompt}\n\n---\n\n{context_text}"

        client = anthropic.AsyncAnthropic(api_key=settings.require_anthropic_key())
        tool_uses: list[ToolUseInfo] = []
        response_text_parts: list[str] = []

        max_iters = 16
        for _ in range(max_iters):
            response = await client.messages.create(
                model=model_id,
                max_tokens=4096,
                system=full_system,
                tools=TOOLS,  # type: ignore[arg-type]
                messages=messages,  # type: ignore[arg-type]
            )
            assistant_blocks = _serialize_assistant_content(response.content)
            await db.save_chat_message(
                conversation_id=conv.id,
                role=ChatMessageRole.ASSISTANT,
                content={"blocks": assistant_blocks},
                question_id=full_id,
            )
            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason != "tool_use":
                for block in response.content:
                    if isinstance(block, TextBlock):
                        response_text_parts.append(block.text)
                break

            tool_results_block: list[dict[str, Any]] = []
            for block in response.content:
                if isinstance(block, TextBlock):
                    response_text_parts.append(block.text)
                elif isinstance(block, ToolUseBlock):
                    tool_input = block.input if isinstance(block.input, dict) else {}
                    result = await _handle_tool(
                        db, block.name, tool_input, conv_id=conv.id, tool_use_id=block.id
                    )
                    tool_uses.append(ToolUseInfo(name=block.name, input=tool_input, result=result))
                    await db.save_chat_message(
                        conversation_id=conv.id,
                        role=ChatMessageRole.TOOL_RESULT,
                        content={
                            "tool_use_id": block.id,
                            "tool_name": block.name,
                            "result": result,
                        },
                        question_id=full_id,
                    )
                    tool_results_block.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
                        }
                    )
            messages.append({"role": "user", "content": tool_results_block})

        return ChatResponse(
            response="\n".join(response_text_parts).strip(),
            tool_uses=tool_uses,
            conversation_id=conv.id,
        )

    try:
        return await asyncio.wait_for(run_turn(), timeout=CHAT_TURN_TIMEOUT_S)
    except TimeoutError:
        log.warning("chat turn hit timeout (%ss); abandoning", CHAT_TURN_TIMEOUT_S)
        return ChatResponse(
            response=f"Turn timed out after {int(CHAT_TURN_TIMEOUT_S)}s.",
            tool_uses=[],
            conversation_id=request.conversation_id or "",
        )
    finally:
        await db.close()


async def handle_chat_stream(request: ChatRequest) -> StreamingResponse:
    """Handle a chat turn with SSE streaming.

    Streams assistant_text_delta events as tokens arrive, tool_use_start /
    tool_use_result events around each tool call, and a final done event.
    Conversations are persisted identically to the non-streaming path.
    """

    async def event_gen() -> AsyncIterator[str]:
        settings = get_settings()
        model_id = MODEL_MAP.get(request.model, MODEL_MAP["sonnet"])
        db = await DB.create(run_id=str(uuid.uuid4()), prod=settings.is_prod_db)
        try:
            project = await db.get_or_create_project(request.workspace)
            db.project_id = project.id
            await db.create_run(
                name="chat",
                question_id=None,
                config={
                    **settings.capture_config(),
                    "origin": "chat",
                    "model": model_id,
                    "chat_model_short": request.model,
                },
            )
            await db.init_budget(CHAT_RUN_BUDGET)

            full_id = await db.resolve_page_id(request.question_id) if request.question_id else None
            conv = await _ensure_conversation(db, request, full_id)
            yield _sse(
                "conversation",
                {"conversation_id": conv.id, "title": conv.title},
            )

            if request.question_id and not full_id:
                yield _sse(
                    "error", {"message": f"No question found matching '{request.question_id}'"}
                )
                yield _sse("done", {})
                return

            prior_messages = await db.list_chat_messages(conv.id)
            replay = _replay_messages_for_api(prior_messages) if prior_messages else []
            messages: list[dict[str, Any]] = replay + list(request.messages)

            await _persist_user_turn(db, conv, request, full_id)

            system_prompt = (PROMPTS_DIR / "api_chat.md").read_text(encoding="utf-8")
            context_text = (
                await build_chat_context(full_id, db) if full_id else "(no question scope)"
            )
            full_system = f"{system_prompt}\n\n---\n\n{context_text}"

            client = anthropic.AsyncAnthropic(api_key=settings.require_anthropic_key())

            max_iters = 16
            for _ in range(max_iters):
                async with client.messages.stream(
                    model=model_id,
                    max_tokens=4096,
                    system=full_system,
                    tools=TOOLS,  # type: ignore[arg-type]
                    messages=messages,  # type: ignore[arg-type]
                ) as stream:
                    async for event in stream:
                        if event.type == "content_block_delta" and isinstance(
                            event.delta, TextDelta
                        ):
                            yield _sse("assistant_text_delta", {"text": event.delta.text})
                        elif event.type == "content_block_start":
                            cb = event.content_block
                            if isinstance(cb, ToolUseBlock):
                                yield _sse(
                                    "tool_use_start",
                                    {"id": cb.id, "name": cb.name},
                                )

                    response = await stream.get_final_message()

                assistant_blocks = _serialize_assistant_content(response.content)
                await db.save_chat_message(
                    conversation_id=conv.id,
                    role=ChatMessageRole.ASSISTANT,
                    content={"blocks": assistant_blocks},
                    question_id=full_id,
                )
                messages.append({"role": "assistant", "content": response.content})

                if response.stop_reason != "tool_use":
                    break

                tool_results_block: list[dict[str, Any]] = []
                for block in response.content:
                    if not isinstance(block, ToolUseBlock):
                        continue
                    tool_input = block.input if isinstance(block.input, dict) else {}
                    result = await _handle_tool(
                        db, block.name, tool_input, conv_id=conv.id, tool_use_id=block.id
                    )
                    yield _sse(
                        "tool_use_result",
                        {"id": block.id, "name": block.name, "result": result},
                    )
                    await db.save_chat_message(
                        conversation_id=conv.id,
                        role=ChatMessageRole.TOOL_RESULT,
                        content={
                            "tool_use_id": block.id,
                            "tool_name": block.name,
                            "result": result,
                        },
                        question_id=full_id,
                    )
                    tool_results_block.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
                        }
                    )
                messages.append({"role": "user", "content": tool_results_block})

            yield _sse("done", {})
        except Exception as exc:
            log.exception("chat stream failed")
            yield _sse("error", {"message": str(exc)})
        finally:
            await db.close()

    return StreamingResponse(event_gen(), media_type="text/event-stream")


async def handle_conversation_events(conversation_id: str) -> StreamingResponse:
    """Long-lived SSE stream of dispatch completion events for one conversation.

    Combined with the ``dispatch_result`` rows persisted by bg handlers,
    this lets the UI render a live "completion chip" when a fire-and-forget
    call finishes.
    """

    async def gen() -> AsyncIterator[str]:
        q = _subscribe_conv_events(conversation_id)
        try:
            yield _sse("subscribed", {"conversation_id": conversation_id})
            while True:
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=30.0)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield _sse(payload["event"], payload["data"])
        finally:
            _unsubscribe_conv_events(conversation_id, q)

    return StreamingResponse(gen(), media_type="text/event-stream")


async def _await_live_dispatches() -> None:
    """Test helper: wait for all in-flight background dispatch tasks."""
    tasks = list(_live_dispatch_tasks)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
