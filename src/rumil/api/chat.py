"""Chat endpoint for the research UI.

Wraps the Anthropic API with tools that operate on the rumil workspace.
The model sees the research context and can search, inspect, create, and
dispatch — the same capabilities as the CC skills layer, exposed via HTTP.
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
from pydantic import BaseModel

from rumil.calls import (
    ASSESS_CALL_CLASSES,
    FindConsiderationsCall,
    IngestCall,
    ScoutAnalogiesCall,
    ScoutEstimatesCall,
    ScoutHypothesesCall,
    ScoutSubquestionsCall,
    WebResearchCall,
)
from rumil.calls.stages import CallRunner
from rumil.context import build_embedding_based_context
from rumil.database import DB
from rumil.embeddings import embed_query, search_pages_by_vector
from rumil.models import (
    CallType,
    ChatConversation,
    ChatMessage,
    ChatMessageRole,
    LinkRole,
    LinkType,
    MoveType,
    Page,
    PageLayer,
    PageType,
    Workspace,
)
from rumil.moves.registry import MOVES
from rumil.pricing import usd_from_usage
from rumil.scraper import scrape_url
from rumil.settings import get_settings
from rumil.summary import build_research_tree
from rumil.views import View, build_view

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
    conversation_id: str | None = None
    open_run_id: str | None = None
    open_page_ids: list[str] = []
    view_mode: str | None = None


class ToolUseInfo(BaseModel):
    name: str
    input: dict[str, Any]
    result: str


class ChatResponse(BaseModel):
    response: str
    tool_uses: list[ToolUseInfo]
    conversation_id: str


class ConversationListItem(BaseModel):
    id: str
    project_id: str
    question_id: str | None
    title: str
    created_at: str
    updated_at: str


class ConversationDetail(BaseModel):
    id: str
    project_id: str
    question_id: str | None
    title: str
    created_at: str
    updated_at: str
    messages: list[dict[str, Any]]


class CreateConversationRequest(BaseModel):
    project_id: str
    question_id: str | None = None
    first_message: str | None = None
    title: str | None = None


class UpdateConversationRequest(BaseModel):
    title: str


def _derive_title(first_user_message: str) -> str:
    """Slugify-ish: first 80 chars of the first user message, trimmed."""
    text = first_user_message.strip().replace("\n", " ")
    if len(text) > 80:
        text = text[:77].rstrip() + "..."
    return text


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
        "name": "get_view",
        "description": (
            "Fetch the structured view of a question — the distilled research "
            "state, organized into sections (core_findings, live_hypotheses, "
            "key_evidence, key_uncertainties, etc.) plus a health block. "
            "Prefer this over scattered get_page calls when the user asks "
            "'what do we know about X' or 'show me the view'. Returns item "
            "summaries (id, headline, scores); use get_view_item for full content."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "question_id": {
                    "type": "string",
                    "description": (
                        "Short ID of the question. Omit to use the current question in scope."
                    ),
                },
                "importance_threshold": {
                    "type": "integer",
                    "description": "Max importance level to include (default 3)",
                },
            },
        },
    },
    {
        "name": "get_view_item",
        "description": (
            "Drill into a specific view item by its short page ID. Returns "
            "the full content, linked considerations, and the item's role "
            "in the view (which section, supporting/opposing direction)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "item_id": {
                    "type": "string",
                    "description": "8-character page ID of the view item",
                },
                "question_id": {
                    "type": "string",
                    "description": (
                        "Short ID of the question whose view to consult "
                        "for role context. Omit to use the current question."
                    ),
                },
            },
            "required": ["item_id"],
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
    {
        "name": "get_considerations",
        "description": (
            "List all considerations (claims linked as evidence) on a question, "
            "sorted by strength. Each entry has the claim's short ID, headline, "
            "link strength (0-5), direction (supports/opposes/neutral), and "
            "the reasoning for why it bears on the question. Use this to trace "
            "the evidence base — much faster than N sequential get_page calls."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "question_id": {
                    "type": "string",
                    "description": (
                        "Short ID of the question. Omit to use the current question in scope."
                    ),
                },
            },
        },
    },
    {
        "name": "get_child_questions",
        "description": (
            "List sub-questions of a question, with each child's judgement "
            "status (has-judgement vs open), link role (direct/structural), "
            "and estimated impact on the parent. Use to understand how the "
            "question decomposes and which branches are still open."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "question_id": {
                    "type": "string",
                    "description": (
                        "Short ID of the parent question. Omit to use the "
                        "current question in scope."
                    ),
                },
            },
        },
    },
    {
        "name": "get_incoming_links",
        "description": (
            "List all pages that point at this page (who cites it, who uses "
            "it as a consideration, who links to it as related, etc.). "
            "Complements get_page, which only shows outgoing links."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "short_id": {
                    "type": "string",
                    "description": "8-character short ID of the page",
                },
            },
            "required": ["short_id"],
        },
    },
    {
        "name": "get_parent_chain",
        "description": (
            "Walk up the child_question chain from a question to its root. "
            "Returns the list of ancestor questions in order (closest parent "
            "first). Useful for understanding where a sub-question sits in "
            "the broader investigation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "short_id": {
                    "type": "string",
                    "description": (
                        "Short ID of the question. Omit to use the current question in scope."
                    ),
                },
            },
        },
    },
    {
        "name": "list_recent_calls",
        "description": (
            "List recent research calls on a question — call type, status "
            "(complete/failed/running), budget used, cost, timestamp, and "
            "result summary. Use to answer 'what's been investigated' or to "
            "find a specific call to inspect via get_call_trace."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "question_id": {
                    "type": "string",
                    "description": (
                        "Short ID of the question. Omit to use the current question in scope."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Max calls to return (default 15)",
                },
            },
        },
    },
    {
        "name": "get_call_trace",
        "description": (
            "Fetch a specific call's trace — its events (what happened inside "
            "the call), LLM exchanges (round-by-round token counts and any "
            "errors), and metadata (status, cost, result summary). Use when "
            "the user asks why a page was created, what a call concluded, or "
            "to debug a failed call."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "call_id": {
                    "type": "string",
                    "description": "8-character short ID of the call",
                },
            },
            "required": ["call_id"],
        },
    },
    {
        "name": "get_run",
        "description": (
            "Fetch a run's metadata — orchestrator, model, config highlights, "
            "scope question, timestamps, total cost, and per-call-type stats. "
            "Use when the user asks 'which orchestrator did this run?', 'what "
            "was this run configured with?', or to see the overall shape of "
            "a trace the user is viewing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "run_id": {
                    "type": "string",
                    "description": "8-character short ID or full UUID of the run",
                },
            },
            "required": ["run_id"],
        },
    },
    {
        "name": "create_claim",
        "description": (
            "Create a new claim — an assertion with supporting reasoning. "
            "Cite other pages inline with [shortid] in content and the tool "
            "auto-creates depends_on/cites links. Optionally link this claim "
            "as a consideration on a question by passing question_id + "
            "strength + direction."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "headline": {
                    "type": "string",
                    "description": "10-20 word self-contained headline",
                },
                "content": {
                    "type": "string",
                    "description": "Full claim text with reasoning. Cite sources with [shortid].",
                },
                "credence": {
                    "type": "integer",
                    "description": "1-9 credence (default 5)",
                },
                "robustness": {
                    "type": "integer",
                    "description": "1-5 robustness (default 1)",
                },
                "question_id": {
                    "type": "string",
                    "description": (
                        "Optional short ID of a question to link this claim to as a consideration."
                    ),
                },
                "strength": {
                    "type": "number",
                    "description": "0-5 consideration strength (default 2.5). Only used if question_id is set.",
                },
                "reasoning": {
                    "type": "string",
                    "description": "Why this claim bears on the question. Only used if question_id is set.",
                },
            },
            "required": ["headline", "content"],
        },
    },
    {
        "name": "create_judgement",
        "description": (
            "Create a judgement on a question — a considered position "
            "synthesising the considerations. Supersedes any prior judgement "
            "on the question. Should engage with considerations on multiple "
            "sides."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "question_id": {
                    "type": "string",
                    "description": "Short ID of the question being judged",
                },
                "headline": {
                    "type": "string",
                    "description": "10-20 word headline stating the position",
                },
                "content": {
                    "type": "string",
                    "description": "Full judgement text, synthesising considerations",
                },
                "credence": {
                    "type": "integer",
                    "description": "1-9 credence (default 5)",
                },
                "robustness": {
                    "type": "integer",
                    "description": "1-5 robustness (default 1)",
                },
                "key_dependencies": {
                    "type": "string",
                    "description": "What this judgement most depends on",
                },
                "sensitivity_analysis": {
                    "type": "string",
                    "description": "What would shift this judgement, and in which direction",
                },
            },
            "required": ["question_id", "headline", "content"],
        },
    },
    {
        "name": "link_pages",
        "description": (
            "Create a link between two existing pages. Supported link types: "
            "'related' (general relation), 'child_question' (sub-question "
            "under a parent question), 'consideration' (claim bearing on a "
            "question — must pass strength)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "from_id": {
                    "type": "string",
                    "description": "Short ID of the source page",
                },
                "to_id": {
                    "type": "string",
                    "description": "Short ID of the target page",
                },
                "link_type": {
                    "type": "string",
                    "enum": ["related", "child_question", "consideration"],
                    "description": "Link type",
                },
                "reasoning": {
                    "type": "string",
                    "description": "Why this link is warranted",
                },
                "strength": {
                    "type": "number",
                    "description": "0-5 strength (consideration links only; default 2.5)",
                },
            },
            "required": ["from_id", "to_id", "link_type"],
        },
    },
    {
        "name": "update_epistemic",
        "description": (
            "Update credence and robustness scores on a claim or judgement "
            "(not questions — questions don't carry scores). Both values "
            "required, plus reasoning."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "short_id": {
                    "type": "string",
                    "description": "Short ID of the page to update",
                },
                "credence": {
                    "type": "integer",
                    "description": "1-9 new credence score",
                },
                "robustness": {
                    "type": "integer",
                    "description": "1-5 new robustness score",
                },
                "reasoning": {
                    "type": "string",
                    "description": "Why this update is warranted",
                },
            },
            "required": ["short_id", "credence", "robustness", "reasoning"],
        },
    },
    {
        "name": "flag_page",
        "description": (
            "Flag a specific page as having something off or wrong with it. "
            "Use when you or the user spot an error, a misstated claim, a "
            "broken reasoning step, or anything suspicious. Flags are "
            "reviewed later — they don't modify the page."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "short_id": {
                    "type": "string",
                    "description": "Short ID of the flagged page",
                },
                "note": {
                    "type": "string",
                    "description": "What seems off",
                },
            },
            "required": ["short_id", "note"],
        },
    },
    {
        "name": "report_duplicate",
        "description": (
            "Flag two pages as duplicates of each other. Use when you notice "
            "the same claim/question exists twice in the workspace."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "page_id_a": {
                    "type": "string",
                    "description": "Short ID of the first duplicate",
                },
                "page_id_b": {
                    "type": "string",
                    "description": "Short ID of the second duplicate",
                },
            },
            "required": ["page_id_a", "page_id_b"],
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


def _serialize_view(view: View) -> dict[str, Any]:
    """Lean JSON payload of a view — item summaries only, not full content."""
    return {
        "question_id": view.question.id[:8],
        "question_full_id": view.question.id,
        "question_headline": view.question.headline,
        "sections": [
            {
                "name": s.name,
                "description": s.description,
                "items": [
                    {
                        "id": item.page.id[:8],
                        "page_type": item.page.page_type.value,
                        "headline": item.page.headline,
                        "credence": item.page.credence,
                        "robustness": item.page.robustness,
                        "importance": item.page.importance,
                        "section": item.section,
                        "direction": next(
                            (lk.direction.value for lk in item.links if lk.direction is not None),
                            None,
                        ),
                    }
                    for item in s.items
                ],
            }
            for s in view.sections
        ],
        "health": {
            "total_pages": view.health.total_pages,
            "missing_credence": view.health.missing_credence,
            "missing_importance": view.health.missing_importance,
            "child_questions_without_judgements": view.health.child_questions_without_judgements,
            "max_depth": view.health.max_depth,
        },
    }


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
        results = await search_pages_by_vector(db, vector, match_count=8, match_threshold=0.3)
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

        call = await db.create_call(CallType.CHAT_DIRECT, scope_page_id=None)
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
                response_parts.append(f"Linked as child of {parent_short}")

        return "\n".join(response_parts)

    if name == "list_workspace":
        questions = await db.get_root_questions(Workspace.RESEARCH)
        if not questions:
            return "No root questions in this workspace."
        lines = [f"{len(questions)} root question(s):\n"]
        for q in questions:
            counts = await db.count_pages_for_question(q.id)
            total = counts.get("considerations", 0) + counts.get("judgements", 0)
            lines.append(f"  [{q.id[:8]}] {q.headline}  ({total} pages)")
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

    if name == "get_view":
        qid_short = tool_input.get("question_id") or scope_question_id[:8]
        importance_threshold = int(tool_input.get("importance_threshold", 3))
        full_id = await db.resolve_page_id(qid_short)
        if not full_id:
            return f"Question '{qid_short}' not found."
        try:
            view = await build_view(db, full_id, importance_threshold=importance_threshold)
        except ValueError as e:
            return str(e)
        payload = _serialize_view(view)
        return json.dumps(payload)

    if name == "get_view_item":
        item_short = tool_input["item_id"]
        qid_short = tool_input.get("question_id") or scope_question_id[:8]
        item_full = await db.resolve_page_id(item_short)
        if not item_full:
            return f"Item '{item_short}' not found."
        item_page = await db.get_page(item_full)
        if not item_page:
            return f"Item '{item_short}' not found."
        question_full = await db.resolve_page_id(qid_short) if qid_short else None
        section_name: str | None = None
        direction: str | None = None
        if question_full:
            try:
                view = await build_view(db, question_full)
                for s in view.sections:
                    for it in s.items:
                        if it.page.id == item_page.id:
                            section_name = s.name
                            for lk in it.links:
                                if lk.direction is not None:
                                    direction = lk.direction.value
                                    break
                            break
                    if section_name:
                        break
            except ValueError:
                pass
        outgoing = await db.get_links_from(item_full)
        incoming = await db.get_links_to(item_full)
        linked_ids = {lk.to_page_id for lk in outgoing} | {lk.from_page_id for lk in incoming}
        linked_pages = await db.get_pages_by_ids(list(linked_ids)) if linked_ids else {}
        payload: dict[str, Any] = {
            "id": item_page.id[:8],
            "full_id": item_page.id,
            "page_type": item_page.page_type.value,
            "headline": item_page.headline,
            "content": item_page.content,
            "abstract": item_page.abstract,
            "credence": item_page.credence,
            "robustness": item_page.robustness,
            "importance": item_page.importance,
            "section": section_name,
            "direction": direction,
            "outgoing_links": [
                {
                    "link_type": lk.link_type.value,
                    "to_id": lk.to_page_id[:8],
                    "to_headline": (
                        linked_pages[lk.to_page_id].headline
                        if lk.to_page_id in linked_pages
                        else None
                    ),
                    "direction": lk.direction.value if lk.direction else None,
                    "role": lk.role.value if lk.role else None,
                }
                for lk in outgoing
            ],
            "incoming_links": [
                {
                    "link_type": lk.link_type.value,
                    "from_id": lk.from_page_id[:8],
                    "from_headline": (
                        linked_pages[lk.from_page_id].headline
                        if lk.from_page_id in linked_pages
                        else None
                    ),
                    "direction": lk.direction.value if lk.direction else None,
                    "role": lk.role.value if lk.role else None,
                }
                for lk in incoming
            ],
        }
        return json.dumps(payload)

    if name == "dispatch_call":
        qid_short = tool_input["question_id"]
        call_type_str = tool_input["call_type"]
        full_id = await db.resolve_page_id(qid_short)
        if not full_id:
            return f"Question '{qid_short}' not found."
        if call_type_str not in _CALL_TYPE_MAP:
            return f"Unknown call type: {call_type_str}"
        question = await db.get_page(full_id)
        headline = question.headline if question else qid_short
        return json.dumps(
            {
                "__async_dispatch__": True,
                "question_id": full_id,
                "headline": headline,
                "call_type": call_type_str,
            }
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
        return json.dumps(
            {
                "__async_research__": True,
                "question_id": full_id,
                "headline": question.headline,
                "budget": budget,
            }
        )

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
                result_parts.append(
                    f"Target question '{target_short}' not found \u2014 source saved but no extraction."
                )
        return "\n".join(result_parts)

    if name == "get_considerations":
        qid_short = tool_input.get("question_id") or scope_question_id[:8]
        full_id = await db.resolve_page_id(qid_short) if qid_short else None
        if not full_id:
            return f"Question '{qid_short}' not found."
        pairs = await db.get_considerations_for_question(full_id)
        if not pairs:
            return f"No considerations on question {qid_short}."
        pairs = sorted(pairs, key=lambda x: x[1].strength or 0, reverse=True)
        lines = [f"{len(pairs)} consideration(s) on question {qid_short} (by strength):\n"]
        for claim, link in pairs:
            direction = link.direction.value if link.direction else "neutral"
            strength = f"{link.strength:.1f}" if link.strength is not None else "?"
            role = link.role.value if link.role else "?"
            lines.append(
                f"  [{claim.id[:8]}] ({direction}, strength={strength}, role={role}) {claim.headline}"
            )
            if link.reasoning:
                lines.append(f"    bearing: {link.reasoning[:200]}")
        return "\n".join(lines)

    if name == "get_child_questions":
        qid_short = tool_input.get("question_id") or scope_question_id[:8]
        full_id = await db.resolve_page_id(qid_short) if qid_short else None
        if not full_id:
            return f"Question '{qid_short}' not found."
        pairs = await db.get_child_questions_with_links(full_id)
        if not pairs:
            return f"No child questions on {qid_short}."
        child_ids = [c.id for c, _ in pairs]
        judgements_by_q = await db.get_judgements_for_questions(child_ids)
        lines = [f"{len(pairs)} child question(s) under {qid_short}:\n"]
        for child, link in pairs:
            role = link.role.value if link.role else "?"
            impact = (
                f", impact={link.impact_on_parent_question}"
                if link.impact_on_parent_question is not None
                else ""
            )
            js = judgements_by_q.get(child.id, [])
            j_note = f" [judgement: {js[0].id[:8]}]" if js else " [no judgement]"
            lines.append(f"  [{child.id[:8]}] ({role}{impact}) {child.headline}{j_note}")
            if link.reasoning:
                lines.append(f"    link reasoning: {link.reasoning[:200]}")
        return "\n".join(lines)

    if name == "get_incoming_links":
        short = tool_input["short_id"]
        full_id = await db.resolve_page_id(short)
        if not full_id:
            return f"Page '{short}' not found."
        links = await db.get_links_to(full_id)
        if not links:
            return f"No incoming links to {short}."
        from_ids = list({l.from_page_id for l in links})
        from_pages = await db.get_pages_by_ids(from_ids)
        lines = [f"{len(links)} incoming link(s) to {short}:"]
        for lk in links:
            src = from_pages.get(lk.from_page_id)
            src_label = (
                f"{lk.from_page_id[:8]} ({src.page_type.value}) {src.headline[:80]}"
                if src
                else lk.from_page_id[:8]
            )
            bits = [lk.link_type.value]
            if lk.strength is not None and lk.link_type == LinkType.CONSIDERATION:
                bits.append(f"strength={lk.strength:.1f}")
            if lk.direction is not None:
                bits.append(f"direction={lk.direction.value}")
            if lk.role is not None and lk.link_type in (
                LinkType.CONSIDERATION,
                LinkType.CHILD_QUESTION,
            ):
                bits.append(f"role={lk.role.value}")
            lines.append(f"  {' '.join(bits)}: {src_label}")
            if lk.reasoning:
                lines.append(f"    reasoning: {lk.reasoning[:200]}")
        return "\n".join(lines)

    if name == "get_parent_chain":
        short = tool_input.get("short_id") or (scope_question_id[:8] if scope_question_id else "")
        full_id = await db.resolve_page_id(short) if short else None
        if not full_id:
            return f"Page '{short}' not found."
        chain: list[Page] = []
        current = full_id
        visited: set[str] = {current}
        for _ in range(8):
            parent = await db.get_parent_question(current)
            if not parent or parent.id in visited:
                break
            chain.append(parent)
            visited.add(parent.id)
            current = parent.id
        if not chain:
            return f"{short} is a root question (no parent chain)."
        lines = [f"Parent chain for {short} (closest first):"]
        for p in chain:
            lines.append(f"  [{p.id[:8]}] {p.headline}")
        return "\n".join(lines)

    if name == "list_recent_calls":
        qid_short = tool_input.get("question_id") or (
            scope_question_id[:8] if scope_question_id else None
        )
        limit = max(1, min(int(tool_input.get("limit", 15)), 50))
        if not qid_short:
            return "list_recent_calls requires a question_id (or scope question in context)."
        full_id = await db.resolve_page_id(qid_short)
        if not full_id:
            return f"Question '{qid_short}' not found."
        rows = (
            await db._execute(
                db.client.table("calls")
                .select(
                    "id,call_type,status,created_at,budget_allocated,budget_used,"
                    "cost_usd,result_summary,run_id"
                )
                .eq("scope_page_id", full_id)
                .order("created_at", desc=True)
                .limit(limit)
            )
        ).data or []
        if not rows:
            return f"No calls on {qid_short}."
        run_orch_cache: dict[str, str | None] = {}

        async def _orch_for(run_id: str | None) -> str | None:
            if not run_id:
                return None
            if run_id in run_orch_cache:
                return run_orch_cache[run_id]
            try:
                run_row = await db.get_run(run_id)
            except Exception:
                log.debug("get_run failed for run_id %s", run_id, exc_info=True)
                run_row = None
            config = (run_row or {}).get("config") or {}
            orch = config.get("orchestrator") if isinstance(config, dict) else None
            run_orch_cache[run_id] = orch
            return orch

        lines = [f"{len(rows)} recent call(s) on {qid_short}:"]
        for r in rows:
            cost = f" ${r['cost_usd']:.3f}" if r.get("cost_usd") else ""
            budget = f" budget={r.get('budget_used') or 0}/{r.get('budget_allocated') or '?'}"
            ts_raw = r.get("created_at", "")
            try:
                ts = datetime.fromisoformat(ts_raw).strftime("%Y-%m-%d %H:%M")
            except (TypeError, ValueError):
                ts = str(ts_raw)[:16]
            call_type = r.get("call_type") or "?"
            status = r.get("status") or "?"
            orch = await _orch_for(r.get("run_id"))
            orch_note = f" orch={orch}" if orch else (" orch=?" if r.get("run_id") else "")
            lines.append(
                f"  [{str(r['id'])[:8]}] {call_type} ({status}){cost}{budget}{orch_note} — {ts}"
            )
            if r.get("result_summary"):
                lines.append(f"    {r['result_summary'][:200]}")
        return "\n".join(lines)

    if name == "get_call_trace":
        cid_short = tool_input["call_id"]
        full_id = await db.resolve_call_id(cid_short)
        if not full_id:
            return f"Call '{cid_short}' not found."
        call = await db.get_call(full_id)
        if not call:
            return f"Call '{cid_short}' not found."
        call_rows = (
            await db._execute(db.client.table("calls").select("run_id").eq("id", full_id))
        ).data or []
        call_run_id = (call_rows[0] or {}).get("run_id") if call_rows else None
        events = await db.get_call_trace(full_id)
        exchanges = await db.get_llm_exchanges(full_id)
        run_line: str | None = None
        if call_run_id:
            try:
                run_row = await db.get_run(call_run_id)
            except Exception:
                log.debug("get_run failed for call %s", full_id, exc_info=True)
                run_row = None
            config = (run_row or {}).get("config") or {}
            orch = config.get("orchestrator") if isinstance(config, dict) else None
            orch_note = f" (orchestrator: {orch})" if orch else ""
            run_line = f"  run: {call_run_id[:8]}{orch_note}"
        lines = [f"Call [{full_id[:8]}] {call.call_type.value} ({call.status.value})"]
        if run_line:
            lines.append(run_line)
        lines.append(
            f"  scope={call.scope_page_id[:8] if call.scope_page_id else '-'} "
            f"budget={call.budget_used}/{call.budget_allocated or '?'} "
            f"cost=${(call.cost_usd or 0):.3f}"
        )
        if call.result_summary:
            lines.append(f"  summary: {call.result_summary[:400]}")
        lines.append(f"\n{len(events)} trace event(s):")
        for ev in events[:40]:
            ev_type = ev.get("event", "?") if isinstance(ev, dict) else "?"
            rest = {k: v for k, v in ev.items() if k != "event"} if isinstance(ev, dict) else {}
            snippet = json.dumps(rest, default=str)[:200]
            lines.append(f"  - {ev_type}: {snippet}")
        if len(events) > 40:
            lines.append(f"  ... ({len(events) - 40} more events)")
        lines.append(f"\n{len(exchanges)} LLM exchange(s):")
        for ex in exchanges[:10]:
            phase = ex.get("phase", "?")
            rnd = ex.get("round", "?")
            tin = ex.get("input_tokens") or 0
            tout = ex.get("output_tokens") or 0
            err = ex.get("error")
            err_note = f" ERROR: {str(err)[:100]}" if err else ""
            lines.append(f"  - {phase} r{rnd}: {tin}->{tout} tok{err_note}")
        return "\n".join(lines)

    if name == "get_run":
        rid = tool_input["run_id"]
        full_run_id: str | None = None
        run_row: dict[str, Any] | None = None
        try:
            direct = await db.get_run(rid)
        except Exception:
            log.debug("get_run direct lookup failed for %s", rid, exc_info=True)
            direct = None
        if direct:
            full_run_id = direct["id"]
            run_row = direct
        else:
            matches = (
                await db._execute(db.client.table("runs").select("*").like("id", f"{rid}%"))
            ).data or []
            if len(matches) == 1:
                first = matches[0]
                if first:
                    run_row = first
                    full_run_id = str(first["id"])
            elif len(matches) > 1:
                return f"Run id '{rid}' is ambiguous ({len(matches)} matches)."
        if not run_row or not full_run_id:
            return f"Run '{rid}' not found."
        config = run_row.get("config") or {}
        if not isinstance(config, dict):
            config = {}
        orch = config.get("orchestrator") or "?"
        model_name = config.get("model") or config.get("model_name") or "?"
        scope_qid = run_row.get("question_id")
        scope_headline: str | None = None
        if scope_qid:
            scope_page = await db.get_page(scope_qid)
            scope_headline = scope_page.headline if scope_page else None
        rows = (
            await db._execute(
                db.client.table("calls")
                .select("call_type,cost_usd,budget_used,status")
                .eq("run_id", full_run_id)
            )
        ).data or []
        total_cost = 0.0
        total_budget = 0
        by_type: dict[str, int] = {}
        by_status: dict[str, int] = {}
        for r in rows:
            cost = r.get("cost_usd")
            if cost is not None:
                total_cost += float(cost)
            used = r.get("budget_used") or 0
            total_budget += int(used)
            ct = r.get("call_type") or "?"
            by_type[ct] = by_type.get(ct, 0) + 1
            st = r.get("status") or "?"
            by_status[st] = by_status.get(st, 0) + 1
        lines = [
            f"Run [{full_run_id[:8]}] {run_row.get('name') or '?'}",
            f"  orchestrator: {orch}",
            f"  model: {model_name}",
        ]
        if scope_qid:
            scope_label = f"{scope_qid[:8]} — {scope_headline}" if scope_headline else scope_qid[:8]
            lines.append(f"  scope question: {scope_label}")
        lines.append(f"  created: {run_row.get('created_at') or '?'}")
        lines.append(f"  staged: {run_row.get('staged')}")
        lines.append(f"  total cost: ${total_cost:.3f}")
        lines.append(f"  total budget used: {total_budget}")
        lines.append(f"  calls: {len(rows)}")
        if by_type:
            type_bits = ", ".join(f"{k}={v}" for k, v in sorted(by_type.items()))
            lines.append(f"    by type: {type_bits}")
        if by_status:
            status_bits = ", ".join(f"{k}={v}" for k, v in sorted(by_status.items()))
            lines.append(f"    by status: {status_bits}")
        interesting_keys = (
            "origin",
            "available_moves",
            "assess_call_variant",
            "git_commit",
            "git_branch",
        )
        extras = [(k, config[k]) for k in interesting_keys if k in config]
        if extras:
            lines.append("  config:")
            for k, v in extras:
                lines.append(f"    {k}: {v}")
        return "\n".join(lines)

    if name == "create_claim":
        headline = tool_input["headline"]
        content = tool_input.get("content", "")
        question_short = tool_input.get("question_id")
        credence = int(tool_input.get("credence", 5))
        robustness = int(tool_input.get("robustness", 1))
        strength = float(tool_input.get("strength", 2.5))
        reasoning = tool_input.get("reasoning", "")

        scope_full_id: str | None = None
        links_payload: list[dict[str, Any]] = []
        if question_short:
            scope_full_id = await db.resolve_page_id(question_short)
            if not scope_full_id:
                return f"Question '{question_short}' not found."
            links_payload.append(
                {
                    "question_id": scope_full_id,
                    "strength": strength,
                    "reasoning": reasoning,
                }
            )

        move_def = MOVES[MoveType.CREATE_CLAIM]
        payload = move_def.schema(
            headline=headline,
            content=content,
            credence=credence,
            robustness=robustness,
            links=links_payload,
        )
        call = await db.create_call(CallType.CHAT_DIRECT, scope_page_id=scope_full_id)
        result = await move_def.execute(payload, call, db)
        return result.message

    if name == "create_judgement":
        question_short = tool_input["question_id"]
        full_id = await db.resolve_page_id(question_short)
        if not full_id:
            return f"Question '{question_short}' not found."
        move_def = MOVES[MoveType.CREATE_JUDGEMENT]
        payload = move_def.schema(
            headline=tool_input["headline"],
            content=tool_input["content"],
            credence=int(tool_input.get("credence", 5)),
            robustness=int(tool_input.get("robustness", 1)),
            key_dependencies=tool_input.get("key_dependencies"),
            sensitivity_analysis=tool_input.get("sensitivity_analysis"),
        )
        call = await db.create_call(CallType.CHAT_DIRECT, scope_page_id=full_id)
        result = await move_def.execute(payload, call, db)
        return result.message

    if name == "link_pages":
        from_short = tool_input["from_id"]
        to_short = tool_input["to_id"]
        link_type = tool_input["link_type"]
        reasoning = tool_input.get("reasoning", "")
        strength = float(tool_input.get("strength", 2.5))

        from_full = await db.resolve_page_id(from_short)
        to_full = await db.resolve_page_id(to_short)
        if not from_full:
            return f"Page '{from_short}' not found."
        if not to_full:
            return f"Page '{to_short}' not found."

        if link_type == "related":
            move_def = MOVES[MoveType.LINK_RELATED]
            payload = move_def.schema(
                from_page_id=from_full,
                to_page_id=to_full,
                reasoning=reasoning,
            )
            scope = None
        elif link_type == "child_question":
            move_def = MOVES[MoveType.LINK_CHILD_QUESTION]
            payload = move_def.schema(
                parent_id=from_full,
                child_id=to_full,
                reasoning=reasoning,
                role=LinkRole.STRUCTURAL,
            )
            scope = from_full
        elif link_type == "consideration":
            move_def = MOVES[MoveType.LINK_CONSIDERATION]
            payload = move_def.schema(
                claim_id=from_full,
                question_id=to_full,
                strength=strength,
                reasoning=reasoning,
                role=LinkRole.DIRECT,
            )
            scope = to_full
        else:
            return f"Unsupported link_type: {link_type}"

        call = await db.create_call(CallType.CHAT_DIRECT, scope_page_id=scope)
        result = await move_def.execute(payload, call, db)
        return result.message

    if name == "update_epistemic":
        short = tool_input["short_id"]
        full_id = await db.resolve_page_id(short)
        if not full_id:
            return f"Page '{short}' not found."
        move_def = MOVES[MoveType.UPDATE_EPISTEMIC]
        payload = move_def.schema(
            page_id=full_id,
            credence=int(tool_input["credence"]),
            robustness=int(tool_input["robustness"]),
            reasoning=tool_input["reasoning"],
        )
        call = await db.create_call(CallType.CHAT_DIRECT, scope_page_id=full_id)
        result = await move_def.execute(payload, call, db)
        return result.message

    if name == "flag_page":
        short = tool_input["short_id"]
        full_id = await db.resolve_page_id(short)
        if not full_id:
            return f"Page '{short}' not found."
        move_def = MOVES[MoveType.FLAG_FUNNINESS]
        payload = move_def.schema(page_id=full_id, note=tool_input["note"])
        call = await db.create_call(CallType.CHAT_DIRECT, scope_page_id=full_id)
        result = await move_def.execute(payload, call, db)
        return f"Flagged {short}: {result.message}"

    if name == "report_duplicate":
        a_short = tool_input["page_id_a"]
        b_short = tool_input["page_id_b"]
        a_full = await db.resolve_page_id(a_short)
        b_full = await db.resolve_page_id(b_short)
        if not a_full:
            return f"Page '{a_short}' not found."
        if not b_full:
            return f"Page '{b_short}' not found."
        move_def = MOVES[MoveType.REPORT_DUPLICATE]
        payload = move_def.schema(page_id_a=a_full, page_id_b=b_full)
        call = await db.create_call(CallType.CHAT_DIRECT, scope_page_id=None)
        result = await move_def.execute(payload, call, db)
        return f"Duplicate reported ({a_short} <-> {b_short}): {result.message}"

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


async def _run_dispatch(
    db: DB,
    params: dict[str, Any],
    on_progress: Callable[[str], Any] | None = None,
) -> str:
    """Run a single call inline with progress updates."""
    question_id = params["question_id"]
    headline = params.get("headline", question_id[:8])
    call_type_str = params["call_type"]

    ct, cls = _CALL_TYPE_MAP[call_type_str]
    call = await db.create_call(ct, scope_page_id=question_id)
    runner = cls(question_id, call, db)

    if on_progress:
        on_progress(f"Running {call_type_str} on '{headline[:40]}'...")

    try:
        await runner.run()
        if on_progress:
            on_progress(f"{call_type_str} call {call.id[:8]} completed")
        return (
            f"{call_type_str} call {call.id[:8]} on '{headline[:40]}' completed. "
            f"Refresh the view to see new findings."
        )
    except Exception as e:
        log.exception("Dispatch call %s failed", call.id[:8])
        return f"{call_type_str} call {call.id[:8]} failed: {e}"


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
            step_summaries.append(
                f"  Step {i + 1}: {call_type_str} \u2014 call {call.id[:8]} completed"
            )
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


_ASYNC_HANDLERS: dict[str, Callable[..., Any]] = {
    "__async_dispatch__": _run_dispatch,
    "__async_research__": _run_research,
}


async def _resolve_async(
    result_str: str,
    db: DB,
    on_progress: Callable[[str], Any] | None = None,
) -> str:
    """If result contains an async sentinel, run the handler inline. Otherwise pass through."""
    for sentinel, handler in _ASYNC_HANDLERS.items():
        if f'"{sentinel}"' in result_str:
            params = json.loads(result_str)
            return await handler(db, params, on_progress=on_progress)
    return result_str


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
    tree = await build_research_tree(question_id, db, max_depth=3, show_run_ids=True)
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


async def _build_ui_state_block(
    db: DB,
    open_run_id: str | None,
    open_page_ids: Sequence[str],
    view_mode: str | None = None,
) -> str:
    """Render a short 'Currently open in UI' block for the system prompt.

    Returns '' if no field yields resolvable context. Degrades gracefully
    when run or pages are missing.
    """
    lines: list[str] = []

    if view_mode:
        lines.append(f"- View mode: {view_mode}")

    if open_run_id:
        full_run_id: str | None = None
        try:
            rows = (
                await db._execute(
                    db.client.table("runs").select("id,config").like("id", f"{open_run_id}%")
                )
            ).data or []
            if len(rows) == 1 and rows[0]:
                full_run_id = str(rows[0]["id"])
                config = rows[0].get("config") or {}
                orch = config.get("orchestrator") if isinstance(config, dict) else None
                orch_note = f" (orchestrator: {orch})" if orch else ""
                lines.append(f"- Viewing trace for run: {full_run_id[:8]}{orch_note}")
            elif len(rows) > 1:
                log.debug("open_run_id '%s' ambiguous (%d matches)", open_run_id, len(rows))
            else:
                log.debug("open_run_id '%s' not found", open_run_id)
        except Exception:
            log.debug("Failed to resolve open_run_id '%s'", open_run_id, exc_info=True)

    if open_page_ids:
        try:
            resolved = await db.resolve_page_ids(list(open_page_ids))
            full_ids = [v for v in resolved.values() if v]
            pages = await db.get_pages_by_ids(full_ids) if full_ids else {}
            rendered: list[str] = []
            for short in open_page_ids[:6]:
                full = resolved.get(short)
                page = pages.get(full) if full else None
                if page:
                    headline = (page.headline or "")[:80]
                    rendered.append(f'  - {page.id[:8]} ({page.page_type.value}, "{headline}")')
            if rendered:
                lines.append("- Open in inspect panel:")
                lines.extend(rendered)
        except Exception:
            log.debug("Failed to resolve open_page_ids %r", list(open_page_ids), exc_info=True)

    if not lines:
        return ""
    header = "## Currently open in UI"
    return "\n".join([header, *lines])


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


def _content_to_text(content: Any) -> str:
    """Extract plain text from a message content field (string or block list)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif hasattr(block, "text"):
                parts.append(str(block.text))  # type: ignore[union-attr]
        return "\n".join(p for p in parts if p)
    return ""


def _serialize_assistant_content(content: Sequence[Any]) -> list[dict[str, Any]]:
    """Convert Anthropic SDK content blocks into JSON-serializable dicts."""
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


async def _persist_user_turn(db: DB, conv: ChatConversation, request: ChatRequest) -> None:
    """Persist the newest user message from the request (if not already persisted).

    Existing messages are addressed by count: if the DB already has N stored
    messages and the request carries >N messages, persist everything new
    (only the user-originated entries; assistant turns are persisted as they
    happen during generation).
    """
    existing = await db.list_chat_messages(conv.id)
    persisted_user_turns = sum(1 for m in existing if m.role == ChatMessageRole.USER)
    incoming_user_turns = [m for m in request.messages if m.get("role") == "user"]
    for idx, m in enumerate(incoming_user_turns):
        if idx < persisted_user_turns:
            continue
        text = _content_to_text(m.get("content"))
        await db.save_chat_message(
            conversation_id=conv.id,
            role=ChatMessageRole.USER,
            content={"text": text},
        )


async def _aggregate_turn_research_cost(
    db: DB,
    turn_start_iso: str,
) -> tuple[float, dict[str, float]]:
    """Sum `cost_usd` across calls created this turn.

    Returns `(research_usd, by_call_type)`. Rows with a NULL `cost_usd` are
    skipped (pending calls, calls without LLM cost). Callers that want to
    distinguish "no cost yet" from "zero cost" should rely on their own
    bookkeeping — this helper collapses both to 0 for display.
    """
    rows = (
        await db._execute(
            db.client.table("calls")
            .select("id,call_type,cost_usd,created_at")
            .eq("run_id", db.run_id)
            .gte("created_at", turn_start_iso)
        )
    ).data or []
    total = 0.0
    by_type: dict[str, float] = {}
    for row in rows:
        cost = row.get("cost_usd")
        if cost is None:
            continue
        cost_f = float(cost)
        total += cost_f
        call_type = row.get("call_type") or "unknown"
        by_type[call_type] = by_type.get(call_type, 0.0) + cost_f
    return total, by_type


async def handle_chat(request: ChatRequest) -> ChatResponse:
    """Handle a chat request: build context, call LLM with tools, return response."""
    settings = get_settings()
    db = await DB.create(
        run_id=str(uuid.uuid4()),
        prod=settings.is_prod_db,
    )
    project, _ = await db.get_or_create_project(request.workspace)
    db.project_id = project.id
    await db.create_run(name="chat", question_id=None, config={"origin": "chat"})

    try:
        full_id = await db.resolve_page_id(request.question_id) if request.question_id else None
        if request.question_id and not full_id:
            conv_stub = await _ensure_conversation(db, request, None)
            return ChatResponse(
                response=f"No question found matching '{request.question_id}'",
                tool_uses=[],
                conversation_id=conv_stub.id,
            )

        conv = await _ensure_conversation(db, request, full_id)

        prior_messages = await db.list_chat_messages(conv.id)
        resume = bool(prior_messages)
        if resume:
            replay = _replay_messages_for_api(prior_messages)
            messages = replay + list(request.messages[len(_user_turns(prior_messages)) :])
        else:
            messages = list(request.messages)

        await _persist_user_turn(db, conv, request)

        system_prompt = (PROMPTS_DIR / "api_chat.md").read_text(encoding="utf-8")
        context_scope_id = full_id or ""
        context_text = await build_chat_context(full_id, db) if full_id else "(no question scope)"
        ui_block = await _build_ui_state_block(db, request.open_run_id, request.open_page_ids, request.view_mode)
        preamble = f"{ui_block}\n\n" if ui_block else ""
        full_system = f"{system_prompt}\n\n---\n\n{preamble}{context_text}"

        model_id = MODEL_MAP.get(request.model, MODEL_MAP["sonnet"])
        client = anthropic.AsyncAnthropic(api_key=settings.require_anthropic_key())
        tool_uses_log: list[ToolUseInfo] = []

        turn_start_iso = datetime.now(UTC).isoformat()
        turn_chat_usd = 0.0

        for _ in range(10):
            response = await client.messages.create(
                model=model_id,
                max_tokens=4096,
                temperature=0.7,
                system=full_system,
                messages=messages,  # type: ignore[arg-type]
                tools=TOOLS,  # type: ignore[arg-type]
            )
            try:
                turn_chat_usd += usd_from_usage(model_id, response.usage)
            except KeyError:
                log.warning("No pricing entry for model %s; chat cost not counted", model_id)

            text_parts: list[str] = []
            tool_calls: list[ToolUseBlock] = []
            for block in response.content:
                if isinstance(block, TextBlock):
                    text_parts.append(block.text)
                elif isinstance(block, ToolUseBlock):
                    tool_calls.append(block)

            assistant_content: dict[str, Any] = {
                "blocks": _serialize_assistant_content(response.content),
            }
            if not tool_calls:
                research_usd, research_by_type = await _aggregate_turn_research_cost(
                    db, turn_start_iso
                )
                assistant_content["costs"] = {
                    "chat_usd": turn_chat_usd,
                    "research_usd": research_usd,
                    "research_by_call_type": research_by_type,
                }
            await db.save_chat_message(
                conversation_id=conv.id,
                role=ChatMessageRole.ASSISTANT,
                content=assistant_content,
            )

            if not tool_calls:
                await db.update_chat_conversation(conv.id, touch=True)
                return ChatResponse(
                    response="\n".join(text_parts),
                    tool_uses=tool_uses_log,
                    conversation_id=conv.id,
                )

            messages.append({"role": "assistant", "content": response.content})

            tool_results = []
            for tc in tool_calls:
                result_str = await _execute_tool(tc.name, tc.input, db, context_scope_id)
                result_str = await _resolve_async(result_str, db)
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

            await db.save_chat_message(
                conversation_id=conv.id,
                role=ChatMessageRole.TOOL_RESULT,
                content={"results": tool_results},
            )

            messages.append({"role": "user", "content": tool_results})

        await db.update_chat_conversation(conv.id, touch=True)
        return ChatResponse(
            response="Reached maximum tool-use rounds.",
            tool_uses=tool_uses_log,
            conversation_id=conv.id,
        )
    finally:
        await db.close()


def _user_turns(messages: Sequence[ChatMessage]) -> list[ChatMessage]:
    return [m for m in messages if m.role == ChatMessageRole.USER]


def _replay_messages_for_api(prior: Sequence[ChatMessage]) -> list[dict[str, Any]]:
    """Convert persisted messages back into Anthropic-API shape for a resume call."""
    out: list[dict[str, Any]] = []
    for m in prior:
        if m.role == ChatMessageRole.USER:
            text = m.content.get("text", "") if isinstance(m.content, dict) else ""
            out.append({"role": "user", "content": text})
        elif m.role == ChatMessageRole.ASSISTANT:
            blocks = m.content.get("blocks", []) if isinstance(m.content, dict) else []
            out.append({"role": "assistant", "content": blocks})
        elif m.role == ChatMessageRole.TOOL_RESULT:
            results = m.content.get("results", []) if isinstance(m.content, dict) else []
            out.append({"role": "user", "content": results})
    return out


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


async def handle_chat_stream(request: ChatRequest) -> StreamingResponse:
    """Handle a streaming chat request, yielding SSE events."""
    settings = get_settings()
    db = await DB.create(
        run_id=str(uuid.uuid4()),
        prod=settings.is_prod_db,
    )
    project, _ = await db.get_or_create_project(request.workspace)
    db.project_id = project.id
    await db.create_run(name="chat", question_id=None, config={"origin": "chat"})

    full_id = await db.resolve_page_id(request.question_id) if request.question_id else None
    if request.question_id and not full_id:

        async def error_gen() -> AsyncIterator[str]:
            yield _sse("error", {"message": f"No question found matching '{request.question_id}'"})

        return StreamingResponse(error_gen(), media_type="text/event-stream")

    conv = await _ensure_conversation(db, request, full_id)

    prior_messages = await db.list_chat_messages(conv.id)
    if prior_messages:
        replay = _replay_messages_for_api(prior_messages)
        messages = replay + list(request.messages[len(_user_turns(prior_messages)) :])
    else:
        messages = list(request.messages)

    await _persist_user_turn(db, conv, request)

    system_prompt = (PROMPTS_DIR / "api_chat.md").read_text(encoding="utf-8")
    context_text = await build_chat_context(full_id, db) if full_id else "(no question scope)"
    ui_block = await _build_ui_state_block(db, request.open_run_id, request.open_page_ids, request.view_mode)
    preamble = f"{ui_block}\n\n" if ui_block else ""
    full_system = f"{system_prompt}\n\n---\n\n{preamble}{context_text}"
    model_id = MODEL_MAP.get(request.model, MODEL_MAP["sonnet"])
    client = anthropic.AsyncAnthropic(api_key=settings.require_anthropic_key())
    context_scope_id = full_id or ""

    async def generate() -> AsyncIterator[str]:
        nonlocal messages
        turn_start_iso = datetime.now(UTC).isoformat()
        turn_chat_usd = 0.0
        try:
            yield _sse("conversation", {"conversation_id": conv.id, "title": conv.title})
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
                try:
                    turn_chat_usd += usd_from_usage(model_id, response.usage)
                except KeyError:
                    log.warning("No pricing entry for model %s; chat cost not counted", model_id)

                tool_calls = [b for b in response.content if isinstance(b, ToolUseBlock)]
                is_terminal = not tool_calls
                assistant_content: dict[str, Any] = {
                    "blocks": _serialize_assistant_content(response.content),
                }
                costs_payload: dict[str, Any] | None = None
                if is_terminal:
                    research_usd, research_by_type = await _aggregate_turn_research_cost(
                        db, turn_start_iso
                    )
                    costs_payload = {
                        "chat_usd": turn_chat_usd,
                        "research_usd": research_usd,
                        "research_by_call_type": research_by_type,
                    }
                    assistant_content["costs"] = costs_payload

                await db.save_chat_message(
                    conversation_id=conv.id,
                    role=ChatMessageRole.ASSISTANT,
                    content=assistant_content,
                )

                if is_terminal and costs_payload is not None:
                    yield _sse("turn_costs", costs_payload)
                    break

                messages.append({"role": "assistant", "content": response.content})

                tool_results = []
                for tc in tool_calls:
                    result_str = await _execute_tool(tc.name, tc.input, db, context_scope_id)
                    has_async = any(f'"{s}"' in result_str for s in _ASYNC_HANDLERS)
                    if has_async:
                        progress_q: asyncio.Queue[str] = asyncio.Queue()
                        task = asyncio.create_task(
                            _resolve_async(
                                result_str, db, on_progress=lambda m: progress_q.put_nowait(m)
                            )
                        )
                        while not task.done():
                            try:
                                msg = await asyncio.wait_for(progress_q.get(), timeout=0.5)
                                yield _sse("orchestrator_progress", {"message": msg})
                            except TimeoutError:
                                continue
                        result_str = await task
                        while not progress_q.empty():
                            yield _sse(
                                "orchestrator_progress", {"message": progress_q.get_nowait()}
                            )
                    yield _sse(
                        "tool_use_result",
                        {
                            "name": tc.name,
                            "input": tc.input,
                            "result": result_str[:500],
                        },
                    )
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tc.id,
                            "content": result_str,
                        }
                    )

                await db.save_chat_message(
                    conversation_id=conv.id,
                    role=ChatMessageRole.TOOL_RESULT,
                    content={"results": tool_results},
                )

                messages.append({"role": "user", "content": tool_results})

            await db.update_chat_conversation(conv.id, touch=True)
            yield _sse("done", {"conversation_id": conv.id})
        except Exception as e:
            log.error("Chat stream error: %s", e, exc_info=True)
            yield _sse("error", {"message": str(e)})
        finally:
            await db.close()

    return StreamingResponse(generate(), media_type="text/event-stream")
