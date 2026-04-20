"""Chat endpoint for the research UI.

Wraps the Anthropic API with tools that operate on the rumil workspace.
The model sees the research context and can search, inspect, create, and
dispatch — the same capabilities as the CC skills layer, exposed via HTTP.
"""

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import anthropic
from anthropic.types import TextBlock, TextDelta, ToolUseBlock
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from rumil.available_moves import get_moves_for_call
from rumil.calls import (
    FindConsiderationsCall,
    IngestCall,
)
from rumil.calls.call_registry import CALL_RUNNER_CLASSES, get_call_runner_class
from rumil.calls.stages import CallRunner
from rumil.context import build_embedding_based_context
from rumil.database import DB
from rumil.embeddings import embed_query, search_pages_by_vector
from rumil.llm import make_anthropic_client
from rumil.models import (
    Call,
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
from rumil.observability import llm_boundary
from rumil.orchestrators import ORCHESTRATORS
from rumil.pricing import usd_from_usage
from rumil.run_executor import RunExecutor
from rumil.scraper import scrape_url
from rumil.settings import get_settings, override_settings
from rumil.summary import build_research_tree
from rumil.views import View, build_view

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

Research calls dispatched from chat (find-considerations, scout-*, etc.)
consume one unit per agent-loop round via `consume_budget`. Without an
initialized budget, every round fails its gate and the call terminates
after context-build with 0 pages created. 50 is plenty of headroom for
several multi-round dispatches per chat turn; actual spend is bounded by
per-call `max_rounds`, not this number.
"""

DEFAULT_DISPATCH_MAX_ROUNDS = 4
MIN_DISPATCH_MAX_ROUNDS = 1
MAX_DISPATCH_MAX_ROUNDS = 8

# Strong refs to in-flight chat-turn tasks so they don't get GC'd if the
# browser disconnects and the SSE generator closure is released. Tasks
# remove themselves on completion.
_live_chat_turns: set[asyncio.Task[None]] = set()


class ChatRequest(BaseModel):
    question_id: str
    messages: list[dict[str, Any]]
    workspace: str = "default"
    model: str = "sonnet"
    conversation_id: str | None = None
    open_run_id: str | None = None
    open_page_ids: list[str] = []
    view_mode: str | None = None
    open_call_id: str | None = None
    drawer_page_id: str | None = None
    active_section: str | None = None
    review_open: bool = False


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
    # Branching metadata. Null for original, non-branched conversations.
    # Surfaced in the list so the sidebar can prefix branches with a "↪"
    # marker without having to fetch each conversation individually.
    parent_conversation_id: str | None = None
    branched_at_seq: int | None = None


class ConversationDetail(BaseModel):
    id: str
    project_id: str
    question_id: str | None
    title: str
    created_at: str
    updated_at: str
    messages: list[dict[str, Any]]
    parent_conversation_id: str | None = None
    branched_at_seq: int | None = None


class CreateConversationRequest(BaseModel):
    project_id: str
    question_id: str | None = None
    first_message: str | None = None
    title: str | None = None


class UpdateConversationRequest(BaseModel):
    title: str


class BranchConversationRequest(BaseModel):
    """Body for POST /api/chat/conversations/{id}/branch.

    Copies every message in the source conversation where seq <= at_seq
    into a brand-new conversation, linked via parent_conversation_id.
    The source is untouched.
    """

    at_seq: int
    # Optional title override. If omitted, the backend auto-generates one
    # in the form "branch of <parent title> @ msg <seq>".
    title: str | None = None


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
            "This tool is FIRE-AND-FORGET: it returns immediately with a "
            "'started' receipt (run_id and trace URL). The actual call runs "
            "in a detached background task. Do NOT wait for or invent a "
            "result in this turn — tell the user the call has been kicked "
            "off, then end your turn or move on to the next action. When "
            "the call finishes, a completion note will be persisted to the "
            "conversation and you'll see it on the next turn. "
            "Available call types: find-considerations, assess, web-research, "
            "scout-subquestions, scout-hypotheses, scout-estimates, scout-analogies. "
            "Effort is controlled by `max_rounds` (default "
            f"{DEFAULT_DISPATCH_MAX_ROUNDS}, min {MIN_DISPATCH_MAX_ROUNDS}, max "
            f"{MAX_DISPATCH_MAX_ROUNDS}) — each round lets the model produce "
            "pages and call moves; more rounds = broader exploration but linearly "
            "more cost. `model` optionally overrides the default model for this "
            "single call (haiku/sonnet/opus) — haiku for a cheap first pass, opus "
            "for a high-quality final pass."
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
                "max_rounds": {
                    "type": "integer",
                    "minimum": MIN_DISPATCH_MAX_ROUNDS,
                    "maximum": MAX_DISPATCH_MAX_ROUNDS,
                    "description": (
                        f"Agent-loop rounds (default {DEFAULT_DISPATCH_MAX_ROUNDS}). "
                        "Higher = deeper investigation, more cost."
                    ),
                },
                "model": {
                    "type": "string",
                    "enum": ["haiku", "sonnet", "opus"],
                    "description": (
                        "Override the default model for this single call. "
                        "Use haiku for a cheap first pass; opus for a high-quality "
                        "final pass."
                    ),
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
        "name": "evaluate",
        "description": (
            "Run an evaluation agent on a question. Produces a markdown "
            "report (stored in the call's review_json.evaluation). "
            "eval_type controls which lens: 'default' checks falsifiable "
            "grounding of claims, 'feedback' surfaces structural/framing "
            "issues, 'grounding' is a legacy variant of default. "
            "COSTS REAL MONEY. Confirm with user first. Not cheap; use "
            "`get_evaluation` to re-read an existing eval rather than rerunning."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "question_id": {
                    "type": "string",
                    "description": "Short ID of the question to evaluate",
                },
                "eval_type": {
                    "type": "string",
                    "description": (
                        "Evaluation lens: 'default' (falsifiable grounding), "
                        "'feedback' (structural), or 'grounding' (legacy). "
                        "Defaults to 'default'."
                    ),
                },
            },
            "required": ["question_id"],
        },
    },
    {
        "name": "ground_evaluation",
        "description": (
            "Apply a follow-up pipeline to an existing evaluation call. "
            "'grounding' runs web research on the grounding gaps and applies "
            "updates to the claims (best with eval_type='default' or 'grounding'). "
            "'feedback' applies structural edits — split/merge/reframe — "
            "recommended by a feedback evaluation (best with eval_type='feedback'). "
            "COSTS REAL MONEY. Confirm first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "eval_call_id": {
                    "type": "string",
                    "description": ("Short ID of the EVALUATE call to apply the pipeline to"),
                },
                "pipeline": {
                    "type": "string",
                    "description": (
                        "'grounding' or 'feedback'. If omitted, defaults to 'grounding'."
                    ),
                },
                "from_stage": {
                    "type": "integer",
                    "description": (
                        "Resume from stage N of the pipeline (1-based). "
                        "Defaults to 1 (start from the beginning)."
                    ),
                },
            },
            "required": ["eval_call_id"],
        },
    },
    {
        "name": "get_evaluation",
        "description": (
            "Read the evaluation text from an existing EVALUATE call. "
            "Free / read-only — use this to inspect an eval without re-running. "
            "Returns the markdown report and metadata (eval_type if known, "
            "scope page, status)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "call_id": {
                    "type": "string",
                    "description": "Short ID of the EVALUATE call",
                },
            },
            "required": ["call_id"],
        },
    },
    {
        "name": "orchestrate",
        "description": (
            "Run a real rumil orchestrator on a question. This is the same "
            "machinery that `main.py --continue` and `/api/questions/{id}/continue` "
            "use — a full multi-call research loop with prioritization, "
            "broadcasting, and budget management. The exact strategy is "
            "controlled by the `orchestrator` variant. Variants are listed in "
            "the orchestrator catalog below. Creates a fresh run_id so the trace "
            "appears cleanly at /traces/{run_id}. "
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
                    "description": "Max number of calls to run (default 3, max 20)",
                },
                "orchestrator": {
                    "type": "string",
                    "description": (
                        "Which orchestrator variant to run. Omit to use the "
                        "system default (two_phase). See the orchestrator "
                        "catalog in the system prompt for tradeoffs."
                    ),
                },
                "available_calls": {
                    "type": "string",
                    "description": (
                        "Override the `available_calls` preset (e.g. 'simple', "
                        "'multi-subquestion'). Omit to use the system default."
                    ),
                },
                "available_moves": {
                    "type": "string",
                    "description": (
                        "Override the `available_moves` preset. Omit to use the system default."
                    ),
                },
                "enable_global_prio": {
                    "type": "boolean",
                    "description": (
                        "Wrap the selected orchestrator in GlobalPrioOrchestrator, "
                        "which spends a fraction of budget on workspace-wide "
                        "prioritization. Defaults to the system setting."
                    ),
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
                    "description": "1-9 credence",
                },
                "credence_reasoning": {
                    "type": "string",
                    "description": "Why this credence level — what would push it up or down.",
                },
                "robustness": {
                    "type": "integer",
                    "description": "1-5 robustness",
                },
                "robustness_reasoning": {
                    "type": "string",
                    "description": "Where the remaining uncertainty stems from and how reducible it is.",
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
            "required": [
                "headline",
                "content",
                "credence",
                "credence_reasoning",
                "robustness",
                "robustness_reasoning",
            ],
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
                "robustness": {
                    "type": "integer",
                    "description": "1-5 robustness",
                },
                "robustness_reasoning": {
                    "type": "string",
                    "description": "Where the remaining uncertainty stems from and how reducible it is.",
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
            "required": [
                "question_id",
                "headline",
                "content",
                "robustness",
                "robustness_reasoning",
            ],
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
    {
        "name": "get_recent_activity",
        "description": (
            "Workspace-level recent activity — recent runs, newly-created "
            "pages, and recent call dispatches in a time window. Use to "
            "answer 'what happened recently in this workspace' or to "
            "ground the conversation in what research has actually been "
            "produced. Optionally scope to a specific question."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "question_id": {
                    "type": "string",
                    "description": (
                        "Optional short ID of a question. If set, scope to "
                        "runs whose question_id matches, pages from those "
                        "runs, and calls scoped to the question."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Max items per sub-list (default 10, max 50)",
                },
                "hours": {
                    "type": "integer",
                    "description": "Time window in hours (default 24, max 720)",
                },
            },
        },
    },
    {
        "name": "set_view",
        "description": (
            "Navigate the user's UI to a specific view. Use after dispatching "
            "a call the user will want to watch (say 'let me take you to the "
            "trace' and call set_view with view='trace' and the new run_id), "
            "or when a discussion is easier to have in a specific view mode. "
            "Use sparingly — only when there's a clear user benefit."
        ),
        "input_schema": {
            "type": "object",
            "required": ["view"],
            "properties": {
                "view": {
                    "type": "string",
                    "enum": [
                        "panes",
                        "article",
                        "vertical",
                        "sections",
                        "sources",
                        "trace",
                    ],
                    "description": "The view mode to switch to.",
                },
                "run_id": {
                    "type": "string",
                    "description": (
                        "Run ID (8-char short or full UUID). Required when view is 'trace'."
                    ),
                },
                "call_id": {
                    "type": "string",
                    "description": (
                        "Optional call ID (8-char short or full UUID) to focus within trace."
                    ),
                },
                "question_id": {
                    "type": "string",
                    "description": (
                        "Optional question ID (short or full UUID). "
                        "If set, navigation also switches to this question."
                    ),
                },
                "panes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": ("Optional list of 8-char short IDs to pin as panes."),
                },
            },
        },
    },
]

_VALID_VIEW_MODES = {"panes", "article", "vertical", "sections", "sources", "trace"}

_CHAT_EXPOSED_CALL_TYPES: tuple[CallType, ...] = (
    CallType.FIND_CONSIDERATIONS,
    CallType.ASSESS,
    CallType.WEB_RESEARCH,
    CallType.SCOUT_SUBQUESTIONS,
    CallType.SCOUT_HYPOTHESES,
    CallType.SCOUT_ESTIMATES,
    CallType.SCOUT_ANALOGIES,
)

_CALL_TYPE_MAP: dict[str, tuple[CallType, type[CallRunner]]] = {
    ct.value.replace("_", "-"): (ct, get_call_runner_class(ct))
    for ct in _CHAT_EXPOSED_CALL_TYPES
    if ct in CALL_RUNNER_CLASSES
}

_CALL_TYPE_CATALOG_INFO: list[tuple[str, CallType, str, str]] = [
    (
        "find-considerations",
        CallType.FIND_CONSIDERATIONS,
        (
            "Multi-round agent loop that surfaces new claims bearing on the "
            "question. Reads existing considerations and workspace neighbors, "
            "creates claims, links them, and may propose view items."
        ),
        "medium",
    ),
    (
        "assess",
        CallType.ASSESS,
        (
            "Reads existing considerations and fills in credence/robustness on "
            "claims. Can create new claims/questions. Under the 'judge-on-assess' "
            "preset it may also issue a judgement."
        ),
        "medium",
    ),
    (
        "web-research",
        CallType.WEB_RESEARCH,
        (
            "The only call type that reaches outside the workspace: searches the "
            "live web via server-side tools and scrapes pages, then turns findings "
            "into claims linked as considerations."
        ),
        "high (live web + scraping)",
    ),
    (
        "scout-subquestions",
        CallType.SCOUT_SUBQUESTIONS,
        (
            "Generates sub-questions that decompose the scope question (as scout "
            "questions, not direct children by default)."
        ),
        "low",
    ),
    (
        "scout-hypotheses",
        CallType.SCOUT_HYPOTHESES,
        ("Generates candidate answers / hypotheses as claims linked as considerations."),
        "low",
    ),
    (
        "scout-estimates",
        CallType.SCOUT_ESTIMATES,
        ("Generates quantitative estimates (e.g. Fermi-style numbers) relevant to the question."),
        "low",
    ),
    (
        "scout-analogies",
        CallType.SCOUT_ANALOGIES,
        (
            "Surfaces analogous past cases or structurally similar situations "
            "that bear on the question."
        ),
        "low",
    ),
]


def _build_call_type_catalog() -> str:
    """Render a markdown catalog of the research calls exposed to chat.

    Move palettes are read from the active ``available_moves`` preset so this
    stays in sync with settings; affordance blurbs and rough cost bands are
    maintained inline in ``_CALL_TYPE_CATALOG_INFO``.
    """
    lines = [
        "## Research call catalog",
        "",
        (
            "The call types you can fire via `dispatch_call` (and that "
            "`start_research` loops over a subset of). Move palettes below "
            "reflect the active `available_moves` preset."
        ),
        "",
    ]
    for cli_name, call_type, affordance, cost in _CALL_TYPE_CATALOG_INFO:
        try:
            moves = get_moves_for_call(call_type)
            move_names = ", ".join(m.value for m in moves) if moves else "(none)"
        except ValueError as exc:
            move_names = f"(no entry in active preset: {exc})"
        lines.append(f"- **{cli_name}** — cost: {cost}")
        lines.append(f"  {affordance}")
        lines.append(f"  Moves: {move_names}")
    return "\n".join(lines)


def _build_orchestrator_catalog() -> str:
    """Render a markdown catalog of orchestrator variants exposed to chat.

    Sourced from ``rumil.orchestrators.registry.ORCHESTRATORS`` — new
    variants appear automatically. Variants marked ``exposed_in_chat=False``
    (e.g. ``refine_artifact``) are omitted; they remain CLI-only.
    """
    lines = [
        "## Orchestrator catalog",
        "",
        (
            "Variants you can pass to the `orchestrator` parameter of "
            "`orchestrate`. Omit to use the system default."
        ),
        "",
    ]
    for variant, spec in sorted(ORCHESTRATORS.items()):
        if not spec.exposed_in_chat:
            continue
        lines.append(f"- **{variant}** — cost: {spec.cost_band}, stability: {spec.stability}")
        lines.append(f"  {spec.description}")
    return "\n".join(lines)


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
        raw_rounds = tool_input.get("max_rounds", DEFAULT_DISPATCH_MAX_ROUNDS)
        max_rounds = max(
            MIN_DISPATCH_MAX_ROUNDS,
            min(int(raw_rounds), MAX_DISPATCH_MAX_ROUNDS),
        )
        model_short = tool_input.get("model")
        model_full = MODEL_MAP[model_short] if model_short in MODEL_MAP else None
        return json.dumps(
            {
                "__async_dispatch__": True,
                "question_id": full_id,
                "headline": headline,
                "call_type": call_type_str,
                "max_rounds": max_rounds,
                "model": model_full,
            }
        )

    if name == "orchestrate":
        qid_short = tool_input["question_id"]
        budget = max(1, min(tool_input.get("budget", 3), 20))
        variant = tool_input.get("orchestrator")
        if variant is not None:
            spec = ORCHESTRATORS.get(variant)
            if spec is None or not spec.exposed_in_chat:
                return (
                    f"Unknown or CLI-only orchestrator variant: {variant!r}. "
                    f"Available: {sorted(v for v, s in ORCHESTRATORS.items() if s.exposed_in_chat)}"
                )
        full_id = await db.resolve_page_id(qid_short)
        if not full_id:
            return f"Question '{qid_short}' not found."
        question = await db.get_page(full_id)
        if not question:
            return f"Question '{qid_short}' not found."
        return json.dumps(
            {
                "__async_orchestrate__": True,
                "question_id": full_id,
                "headline": question.headline,
                "budget": budget,
                "orchestrator": variant,
                "available_calls": tool_input.get("available_calls"),
                "available_moves": tool_input.get("available_moves"),
                "enable_global_prio": tool_input.get("enable_global_prio"),
            }
        )

    if name == "ingest_source":
        url = tool_input["url"]
        target_short = tool_input.get("target_question_id")
        headline = tool_input.get("headline") or url
        return json.dumps(
            {
                "__async_ingest__": True,
                "url": url,
                "target_question_id": target_short,
                "headline": headline,
            }
        )

    if name == "evaluate":
        from rumil.evaluate.registry import EVALUATION_TYPES

        qid_short = tool_input["question_id"]
        eval_type = tool_input.get("eval_type", "default")
        if eval_type not in EVALUATION_TYPES:
            return f"Unknown eval_type: {eval_type!r}. Available: {sorted(EVALUATION_TYPES)}"
        full_id = await db.resolve_page_id(qid_short)
        if not full_id:
            return f"Question '{qid_short}' not found."
        question = await db.get_page(full_id)
        if not question:
            return f"Question '{qid_short}' not found."
        return json.dumps(
            {
                "__async_evaluate__": True,
                "question_id": full_id,
                "headline": question.headline,
                "eval_type": eval_type,
            }
        )

    if name == "ground_evaluation":
        from rumil.evaluate.registry import GROUNDING_PIPELINES

        call_short = tool_input["eval_call_id"]
        pipeline = tool_input.get("pipeline", "grounding")
        from_stage = tool_input.get("from_stage", 1)
        if pipeline not in GROUNDING_PIPELINES:
            return f"Unknown pipeline: {pipeline!r}. Available: {sorted(GROUNDING_PIPELINES)}"
        return json.dumps(
            {
                "__async_ground__": True,
                "eval_call_id": call_short,
                "pipeline": pipeline,
                "from_stage": from_stage,
            }
        )

    if name == "get_evaluation":
        call_short = tool_input["call_id"]
        full_id = await db.resolve_call_id(call_short)
        if not full_id:
            return f"Call '{call_short}' not found."
        call = await db.get_call(full_id)
        if not call:
            return f"Call '{call_short}' not found."
        if call.call_type != CallType.EVALUATE:
            return (
                f"Call {call.id[:8]} is a {call.call_type.value}, not an "
                "EVALUATE call — no evaluation text to return."
            )
        evaluation_text = (call.review_json or {}).get("evaluation", "")
        if not evaluation_text:
            return (
                f"Evaluation call {call.id[:8]} has no 'evaluation' field in "
                f"review_json (status={call.status.value})."
            )
        scope = call.scope_page_id or ""
        scope_short = scope[:8] if scope else "(none)"
        eval_type = (call.call_params or {}).get("eval_type", "unknown")
        return (
            f"# Evaluation (call {call.id[:8]}, eval_type={eval_type}, "
            f"scope={scope_short}, status={call.status.value})\n\n"
            f"{evaluation_text}"
        )

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
        content = tool_input["content"]
        question_short = tool_input.get("question_id")
        credence = int(tool_input["credence"])
        credence_reasoning = tool_input["credence_reasoning"]
        robustness = int(tool_input["robustness"])
        robustness_reasoning = tool_input["robustness_reasoning"]
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
            credence_reasoning=credence_reasoning,
            robustness=robustness,
            robustness_reasoning=robustness_reasoning,
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
            robustness=int(tool_input["robustness"]),
            robustness_reasoning=tool_input["robustness_reasoning"],
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

    if name == "get_recent_activity":
        qid_short = tool_input.get("question_id")
        limit = max(1, min(int(tool_input.get("limit", 10)), 50))
        hours = max(1, min(int(tool_input.get("hours", 24)), 720))
        cutoff = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()

        full_qid: str | None = None
        if qid_short:
            full_qid = await db.resolve_page_id(qid_short)
            if not full_qid:
                return f"Question '{qid_short}' not found."

        runs_query = (
            db.client.table("runs")
            .select("id, name, question_id, config, created_at, staged")
            .gte("created_at", cutoff)
            .order("created_at", desc=True)
            .limit(limit)
        )
        if db.project_id:
            runs_query = runs_query.eq("project_id", db.project_id)
        if full_qid:
            runs_query = runs_query.eq("question_id", full_qid)
        run_rows = (await db._execute(runs_query)).data or []

        cost_query = db.client.table("calls").select("run_id, cost_usd").gte("created_at", cutoff)
        if db.project_id:
            cost_query = cost_query.eq("project_id", db.project_id)
        run_ids_in_window = [r["id"] for r in run_rows]
        if run_ids_in_window:
            cost_query = cost_query.in_("run_id", run_ids_in_window)
        cost_rows = (await db._execute(cost_query)).data or []
        cost_by_run: dict[str, float] = {}
        for row in cost_rows:
            rid = row.get("run_id")
            if not rid:
                continue
            cost_by_run[rid] = cost_by_run.get(rid, 0.0) + float(row.get("cost_usd") or 0.0)

        question_page_ids = [r["question_id"] for r in run_rows if r.get("question_id")]
        question_pages = (
            await db.get_pages_by_ids(list(set(question_page_ids))) if question_page_ids else {}
        )
        recent_runs: list[dict[str, Any]] = []
        for r in run_rows:
            config = r.get("config") or {}
            if not isinstance(config, dict):
                config = {}
            qid = r.get("question_id")
            q_page = question_pages.get(qid) if qid else None
            recent_runs.append(
                {
                    "run_id": str(r["id"])[:8],
                    "name": r.get("name") or "",
                    "orchestrator": config.get("orchestrator"),
                    "cost_usd": round(cost_by_run.get(r["id"], 0.0), 4),
                    "created_at": r.get("created_at"),
                    "staged": bool(r.get("staged")),
                    "question_summary": q_page.headline if q_page else None,
                }
            )

        pages_query = (
            db.client.table("pages")
            .select("id, page_type, headline, created_at, run_id, is_superseded")
            .gte("created_at", cutoff)
            .order("created_at", desc=True)
            .limit(limit * 4)
        )
        if db.project_id:
            pages_query = pages_query.eq("project_id", db.project_id)
        pages_query = db._staged_filter(pages_query)
        if full_qid:
            scoped_runs_query = db.client.table("runs").select("id").eq("question_id", full_qid)
            if db.project_id:
                scoped_runs_query = scoped_runs_query.eq("project_id", db.project_id)
            scoped_run_rows = (await db._execute(scoped_runs_query)).data or []
            scoped_run_ids = [row["id"] for row in scoped_run_rows]
            if scoped_run_ids:
                pages_query = pages_query.in_("run_id", scoped_run_ids)
            else:
                pages_query = pages_query.eq("run_id", "__none__")
        page_rows = (await db._execute(pages_query)).data or []
        recent_pages: list[dict[str, Any]] = []
        for p in page_rows:
            if p.get("is_superseded"):
                continue
            recent_pages.append(
                {
                    "page_id": str(p["id"])[:8],
                    "page_type": p.get("page_type"),
                    "headline": p.get("headline") or "",
                    "created_at": p.get("created_at"),
                    "run_id": str(p["run_id"])[:8] if p.get("run_id") else None,
                }
            )
            if len(recent_pages) >= limit:
                break

        calls_query = (
            db.client.table("calls")
            .select("id, call_type, scope_page_id, created_at")
            .gte("created_at", cutoff)
            .order("created_at", desc=True)
            .limit(limit)
        )
        if db.project_id:
            calls_query = calls_query.eq("project_id", db.project_id)
        if full_qid:
            calls_query = calls_query.eq("scope_page_id", full_qid)
        call_rows = (await db._execute(calls_query)).data or []
        recent_dispatches = [
            {
                "call_id": str(c["id"])[:8],
                "call_type": c.get("call_type"),
                "scope_page_id": (str(c["scope_page_id"])[:8] if c.get("scope_page_id") else None),
                "created_at": c.get("created_at"),
            }
            for c in call_rows
        ]

        payload = {
            "window_hours": hours,
            "scope_question_id": full_qid[:8] if full_qid else None,
            "recent_runs": recent_runs,
            "recent_pages": recent_pages,
            "recent_dispatches": recent_dispatches,
        }
        return json.dumps(payload)

    if name == "set_view":
        return await _execute_set_view(tool_input, db)

    return f"Unknown tool: {name}"


async def _resolve_run_id(db: DB, run_id: str) -> str | None:
    """Resolve a run ID to a full UUID. Accepts full UUIDs or 8-char prefixes.

    Scoped to the active project when one is set, so the same short prefix
    in two projects doesn't collide.
    """
    if not run_id:
        return None
    query = db.client.table("runs").select("id").eq("id", run_id)
    if db.project_id:
        query = query.eq("project_id", db.project_id)
    rows = (await db._execute(query)).data or []
    if rows:
        return rows[0]["id"]
    if len(run_id) <= 8:
        query = db.client.table("runs").select("id").like("id", f"{run_id}%")
        if db.project_id:
            query = query.eq("project_id", db.project_id)
        rows = (await db._execute(query)).data or []
        if len(rows) == 1:
            return rows[0]["id"]
    return None


async def _execute_set_view(tool_input: dict[str, Any], db: DB) -> str:
    """Build a navigation directive for the frontend.

    Returns a JSON string containing either `__navigate__` (on success) or
    `error` (on validation failure). The frontend parses tool_use_result
    payloads looking for `__navigate__` and calls its onNavigate callback.
    """
    view = tool_input.get("view")
    if not isinstance(view, str) or view not in _VALID_VIEW_MODES:
        return json.dumps(
            {
                "error": (
                    f"Invalid view '{view}'. Must be one of: "
                    + ", ".join(sorted(_VALID_VIEW_MODES))
                )
            }
        )

    run_id_in = tool_input.get("run_id")
    call_id_in = tool_input.get("call_id")
    question_id_in = tool_input.get("question_id")
    panes_in = tool_input.get("panes")

    if view == "trace" and not run_id_in:
        return json.dumps({"error": "view='trace' requires a run_id (short or full UUID)."})

    full_run_id: str | None = None
    run_id_short: str | None = None
    if run_id_in:
        full_run_id = await _resolve_run_id(db, run_id_in)
        if not full_run_id:
            return json.dumps({"error": f"Run '{run_id_in}' not found."})
        run_id_short = full_run_id[:8]

    full_call_id: str | None = None
    call_id_short: str | None = None
    if call_id_in:
        full_call_id = await db.resolve_call_id(call_id_in)
        if not full_call_id:
            return json.dumps({"error": f"Call '{call_id_in}' not found."})
        call_id_short = full_call_id[:8]

    full_question_id: str | None = None
    question_id_short: str | None = None
    if question_id_in:
        full_question_id = await db.resolve_page_id(question_id_in)
        if not full_question_id:
            return json.dumps({"error": f"Question '{question_id_in}' not found."})
        question_id_short = full_question_id[:8]

    normalized_panes: list[str] = []
    if panes_in is not None:
        if not isinstance(panes_in, list):
            return json.dumps({"error": "panes must be a list of short IDs."})
        for raw in panes_in:
            if not isinstance(raw, str):
                return json.dumps({"error": f"Invalid pane entry: {raw!r}"})
            normalized = raw.strip().lower()
            if len(normalized) > 8:
                normalized = normalized[:8]
            if len(normalized) != 8 or any(c not in "0123456789abcdef" for c in normalized):
                return json.dumps({"error": f"Invalid pane '{raw}': expected 8-char hex short ID."})
            normalized_panes.append(normalized)

    if view == "trace":
        assert run_id_short is not None
        message = f"Navigating to trace view for run {run_id_short}…"
    else:
        message = f"Switching to {view} view."

    directive: dict[str, Any] = {
        "view": view,
        "run_id": full_run_id,
        "run_id_short": run_id_short,
        "call_id": full_call_id,
        "call_id_short": call_id_short,
        "question_id": full_question_id,
        "question_id_short": question_id_short,
        "panes": normalized_panes,
    }
    return json.dumps({"__navigate__": directive, "message": message})


async def _execute_tool_timed(
    name: str,
    tool_input: dict[str, Any],
    db: DB,
    scope_question_id: str = "",
) -> str:
    """Call _execute_tool, logging client-disconnect cancellations with timing.

    Wraps the single tool-dispatch site so operators can spot slow tools that
    the user gave up on — e.g. a 499 after 8s on an ingest_source URL fetch.
    Re-raises CancelledError so the request terminates cleanly.
    """
    t0 = time.monotonic()
    try:
        return await _execute_tool(name, tool_input, db, scope_question_id)
    except asyncio.CancelledError:
        elapsed_ms = (time.monotonic() - t0) * 1000
        log.warning(
            "Tool %s cancelled mid-execution (elapsed %.0fms, likely client disconnect)",
            name,
            elapsed_ms,
            extra={
                "tool_name": name,
                "elapsed_ms": elapsed_ms,
                "tool_input": tool_input,
            },
        )
        raise


_live_dispatch_tasks: set[asyncio.Task[None]] = set()


_conv_brokers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = {}


def _publish_conv_event(conv_id: str, event_type: str, data: dict[str, Any]) -> None:
    """Publish an event to every subscriber of ``conv_id``'s event stream.

    The broker is a simple in-memory fan-out — each subscriber of the
    conversation's long-lived SSE stream has a queue registered here.
    Publishing is non-blocking (``put_nowait``); if a subscriber's queue
    fills up we drop the event for that subscriber rather than block the
    publisher. Subscribers are removed when their endpoint tears down.
    """
    subscribers = _conv_brokers.get(conv_id)
    if not subscribers:
        return
    payload = {"event": event_type, "data": data}
    for q in list(subscribers):
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            log.warning("Dropping conv event for full queue (conv %s)", conv_id[:8])


def _subscribe_conv(conv_id: str) -> asyncio.Queue[dict[str, Any]]:
    q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=64)
    _conv_brokers.setdefault(conv_id, set()).add(q)
    return q


def _unsubscribe_conv(conv_id: str, q: asyncio.Queue[dict[str, Any]]) -> None:
    subscribers = _conv_brokers.get(conv_id)
    if subscribers is None:
        return
    subscribers.discard(q)
    if not subscribers:
        _conv_brokers.pop(conv_id, None)


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
    model: str | None,
) -> None:
    """Background task: run one dispatch call and persist completion.

    Uses a fresh DB connection because the chat turn's DB closes when the
    turn ends. Writes a ``DISPATCH_RESULT`` message onto the conversation
    on completion (or failure) so the next chat turn sees the outcome and
    the UI can render a chip. Never raises — errors become a failed
    completion row.
    """
    from rumil.dispatch import dispatch_single_call

    if call_type_str not in _CALL_TYPE_MAP:
        log.error("Background dispatch got unknown call_type %r", call_type_str)
        return
    call_type, _cls = _CALL_TYPE_MAP[call_type_str]

    settings = get_settings()
    bg_db = await DB.create(
        run_id=new_run_id,
        prod=settings.is_prod_db,
        project_id=project_id,
    )
    trace_url = f"/traces/{new_run_id}"
    content: dict[str, Any]
    try:
        await bg_db.create_run(
            name=f"chat dispatch: {call_type_str} on {headline[:60]}",
            question_id=question_id,
            config=settings.capture_config(),
        )
        extra: dict[str, Any] = {}
        if call_type == CallType.FIND_CONSIDERATIONS:
            extra["fruit_threshold"] = 4
        try:
            call = await dispatch_single_call(
                call_type,
                question_id,
                bg_db,
                max_rounds=max_rounds,
                model=model,
                extra_runner_kwargs=extra,
            )
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
            log.exception(
                "Background dispatch %s failed (run %s)",
                call_type_str,
                new_run_id[:8],
            )
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


async def _run_dispatch(
    db: DB,
    params: dict[str, Any],
    *,
    conv_id: str | None = None,
    tool_use_id: str | None = None,
    on_progress: Callable[[str], Any] | None = None,
) -> str:
    """Fire-and-forget dispatch.

    Spawns a detached background task that runs ``dispatch_single_call``
    and writes a ``DISPATCH_RESULT`` message on completion. Returns a
    receipt string immediately — this is what the LLM sees as the
    tool_result for this turn.

    ``on_progress`` is accepted for uniform handler signature but unused:
    progress events of an in-flight dispatch land on the per-conversation
    SSE channel (phase 2), not this turn's event queue.
    """
    question_id = params["question_id"]
    headline = params.get("headline", question_id[:8])
    call_type_str = params["call_type"]
    max_rounds = params.get("max_rounds", DEFAULT_DISPATCH_MAX_ROUNDS)
    model = params.get("model")

    if call_type_str not in _CALL_TYPE_MAP:
        return f"Unknown call type: {call_type_str}"
    if not conv_id or not tool_use_id:
        return (
            f"Internal error: dispatch of {call_type_str} is missing "
            f"conversation context. Not running."
        )
    if not db.project_id:
        return f"Internal error: dispatch of {call_type_str} has no project scope."

    new_run_id = str(uuid.uuid4())
    trace_url = f"/traces/{new_run_id}"

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
            model=model,
        )
    )

    return (
        f"{call_type_str} call on '{headline[:40]}' started in background. "
        f"Trace: {trace_url}. A completion note will appear in chat once "
        f"the call finishes — you will not see the result in this turn."
    )


async def _await_live_dispatches() -> None:
    """Test helper: wait for all in-flight background dispatch tasks."""
    tasks = list(_live_dispatch_tasks)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _persist_dispatch_completion(
    bg_db: DB,
    *,
    conv_id: str,
    content: dict[str, Any],
    question_id: str | None,
) -> None:
    """Write a DISPATCH_RESULT chat message and publish the event.

    Shared by every fire-and-forget handler's completion path so the row
    shape and broker notification stay uniform. Swallows DB errors (and
    logs) so a persistence blip can't crash the background task — the
    broker still gets the event.
    """
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
    _publish_conv_event(conv_id, "dispatch_completed", content)


def _spawn_bg(coro: Any) -> None:
    """Register a fire-and-forget background task in the global set."""
    task = asyncio.create_task(coro)
    _live_dispatch_tasks.add(task)
    task.add_done_callback(_live_dispatch_tasks.discard)


async def _bg_run_orchestrate(
    *,
    conv_id: str,
    tool_use_id: str,
    new_run_id: str,
    project_id: str,
    question_id: str,
    headline: str,
    budget: int,
    variant: str | None,
    available_calls: Any,
    available_moves: Any,
    enable_global_prio: Any,
) -> None:
    """Background task: run the orchestrator and persist completion.

    Subscribes to the trace broadcast channel so per-step progress can be
    forwarded to the conversation event stream (phase 3+). On finish
    (success or failure) writes a DISPATCH_RESULT row keyed to
    ``tool_use_id`` and publishes ``dispatch_completed`` on the broker.
    """
    from rumil.dispatch import dispatch_orchestrator
    from rumil.tracing.broadcast import Broadcaster
    from rumil.tracing.subscribe import format_trace_event, stream_run_events

    settings = get_settings()
    new_db = await DB.create(
        run_id=new_run_id,
        prod=settings.is_prod_db,
        project_id=project_id,
    )

    broadcaster: Broadcaster | None = None
    subscription_task: asyncio.Task[None] | None = None
    subscribed_event: asyncio.Event | None = None
    try:
        supabase_url, supabase_key = settings.get_supabase_credentials(prod=settings.is_prod_db)
        broadcaster = Broadcaster(new_run_id, supabase_url, supabase_key)

        def _forward(payload: dict[str, Any]) -> None:
            msg = format_trace_event(payload)
            if msg is None:
                return
            _publish_conv_event(
                conv_id,
                "dispatch_progress",
                {"tool_use_id": tool_use_id, "run_id": new_run_id, "message": msg},
            )

        subscribed_event = asyncio.Event()
        subscription_task = asyncio.create_task(
            stream_run_events(
                new_run_id,
                supabase_url,
                supabase_key,
                _forward,
                subscribed=subscribed_event,
            )
        )
    except Exception as e:
        log.warning(
            "trace broadcast setup failed for run %s (orchestrator will still run): %s",
            new_run_id[:8],
            e,
        )
        broadcaster = None
        subscription_task = None
        subscribed_event = None

    trace_url = f"/traces/{new_run_id}"
    content: dict[str, Any]
    try:
        await new_db.init_budget(budget)
        await new_db.create_run(
            name=f"orchestrate (chat): {headline[:90]}",
            question_id=question_id,
            config=settings.capture_config(),
        )
        if subscribed_event is not None:
            try:
                await asyncio.wait_for(subscribed_event.wait(), timeout=3.0)
            except TimeoutError:
                log.debug(
                    "trace subscription for run %s didn't become ready in 3s; "
                    "orchestrator running without live stream",
                    new_run_id[:8],
                )
        try:
            await dispatch_orchestrator(
                question_id,
                new_db,
                variant=variant,
                available_calls=available_calls,
                available_moves=available_moves,
                enable_global_prio=enable_global_prio,
                broadcaster=broadcaster,
            )
            content = {
                "tool_use_id": tool_use_id,
                "run_id": new_run_id,
                "call_type": "orchestrate",
                "question_id": question_id,
                "headline": headline,
                "status": "completed",
                "summary": (
                    f"Orchestrator run {new_run_id[:8]} on '{headline[:40]}' "
                    f"completed (budget={budget})."
                ),
                "trace_url": trace_url,
            }
        except Exception as e:
            log.exception("Orchestrator run %s failed", new_run_id[:8])
            content = {
                "tool_use_id": tool_use_id,
                "run_id": new_run_id,
                "call_type": "orchestrate",
                "question_id": question_id,
                "headline": headline,
                "status": "failed",
                "summary": f"Orchestrator run {new_run_id[:8]} failed: {e}",
                "error": str(e),
                "trace_url": trace_url,
            }

        await _persist_dispatch_completion(
            new_db,
            conv_id=conv_id,
            content=content,
            question_id=question_id,
        )
    finally:
        if subscription_task is not None:
            subscription_task.cancel()
            try:
                await subscription_task
            except (asyncio.CancelledError, Exception) as e:
                if not isinstance(e, asyncio.CancelledError):
                    log.debug("subscription task cleanup error (non-fatal): %s", e)
        if broadcaster is not None:
            try:
                await broadcaster.close()
            except Exception as e:
                log.debug("broadcaster close error (non-fatal): %s", e)
        await new_db.close()


async def _run_orchestrate(
    db: DB,
    params: dict[str, Any],
    on_progress: Callable[[str], Any] | None = None,
    *,
    conv_id: str | None = None,
    tool_use_id: str | None = None,
) -> str:
    """Fire-and-forget orchestrator run.

    Validates inputs, spawns a background task (which creates its own
    DB + run_id + broadcaster), and returns a receipt for the chat turn.
    Progress events from the orchestrator land on the conversation SSE
    stream via ``dispatch_progress``; the final outcome lands as a
    DISPATCH_RESULT row plus a ``dispatch_completed`` event.
    """
    question_id = params["question_id"]
    headline = params.get("headline", question_id[:8])
    budget = params.get("budget", 3)
    variant = params.get("orchestrator")
    available_calls = params.get("available_calls")
    available_moves = params.get("available_moves")
    enable_global_prio = params.get("enable_global_prio")

    if not conv_id or not tool_use_id:
        return "Internal error: orchestrate tool is missing conversation context."
    if not db.project_id:
        return "Internal error: orchestrate tool has no project scope."

    new_run_id = str(uuid.uuid4())
    settings = get_settings()
    variant_label = variant or settings.prioritizer_variant

    _spawn_bg(
        _bg_run_orchestrate(
            conv_id=conv_id,
            tool_use_id=tool_use_id,
            new_run_id=new_run_id,
            project_id=db.project_id,
            question_id=question_id,
            headline=headline,
            budget=budget,
            variant=variant,
            available_calls=available_calls,
            available_moves=available_moves,
            enable_global_prio=enable_global_prio,
        )
    )
    return (
        f"Orchestrator run started on '{headline[:40]}' "
        f"(variant={variant_label}, budget={budget}). "
        f"Trace: /traces/{new_run_id}. Running in background — you will "
        f"not see results in this turn."
    )


async def _bg_run_ingest(
    *,
    conv_id: str,
    tool_use_id: str,
    new_run_id: str,
    project_id: str,
    url: str,
    target_short: str | None,
    headline: str,
) -> None:
    """Background task: scrape a URL, save as source, and run ingest."""
    settings = get_settings()
    bg_db = await DB.create(
        run_id=new_run_id,
        prod=settings.is_prod_db,
        project_id=project_id,
    )
    trace_url = f"/traces/{new_run_id}"
    content: dict[str, Any]
    target_question_id: str | None = None
    try:
        await bg_db.create_run(
            name=f"ingest (chat): {headline[:90]}",
            question_id=None,
            config=settings.capture_config(),
        )
        try:
            scraped = await scrape_url(url)
            if not scraped:
                raise RuntimeError(f"Failed to fetch URL: {url}")

            source_page = Page(
                page_type=PageType.SOURCE,
                layer=PageLayer.SQUIDGY,
                workspace=Workspace.RESEARCH,
                headline=scraped.title or url,
                content=scraped.content,
                extra={"url": url},
                project_id=project_id,
            )
            await bg_db.save_page(source_page)
            summary_parts = [
                f"Created source page {source_page.id[:8]}: {(scraped.title or url)[:60]}"
            ]

            if target_short:
                full_id = await bg_db.resolve_page_id(target_short)
                if not full_id:
                    summary_parts.append(
                        f"Target question '{target_short}' not found \u2014 "
                        f"source saved but no extraction."
                    )
                else:
                    target_question_id = full_id
                    call = await bg_db.create_call(CallType.INGEST, scope_page_id=full_id)
                    runner = IngestCall(source_page, full_id, call, bg_db)
                    try:
                        await runner.run()
                        summary_parts.append(
                            f"Ingest extraction call {call.id[:8]} on '{headline[:40]}' completed."
                        )
                    except Exception as e:
                        log.exception("Ingest call %s failed", call.id[:8])
                        summary_parts.append(f"Ingest call {call.id[:8]} failed: {e}")

            content = {
                "tool_use_id": tool_use_id,
                "run_id": new_run_id,
                "call_type": "ingest",
                "question_id": target_question_id,
                "headline": headline,
                "status": "completed",
                "summary": " \u2014 ".join(summary_parts),
                "trace_url": trace_url,
                "source_page_id": source_page.id,
                "url": url,
            }
        except Exception as e:
            log.exception("Ingest for %s failed", url)
            content = {
                "tool_use_id": tool_use_id,
                "run_id": new_run_id,
                "call_type": "ingest",
                "question_id": target_question_id,
                "headline": headline,
                "status": "failed",
                "summary": f"Ingest for {url} failed: {e}",
                "error": str(e),
                "trace_url": trace_url,
                "url": url,
            }

        await _persist_dispatch_completion(
            bg_db,
            conv_id=conv_id,
            content=content,
            question_id=target_question_id,
        )
    finally:
        await bg_db.close()


async def _run_ingest(
    db: DB,
    params: dict[str, Any],
    on_progress: Callable[[str], Any] | None = None,
    *,
    conv_id: str | None = None,
    tool_use_id: str | None = None,
) -> str:
    """Fire-and-forget ingest: scrape a URL + run extraction in background."""
    url = params["url"]
    target_short = params.get("target_question_id")
    headline = params.get("headline") or url

    if not conv_id or not tool_use_id:
        return "Internal error: ingest tool is missing conversation context."
    if not db.project_id:
        return "Internal error: ingest tool has no project scope."

    new_run_id = str(uuid.uuid4())
    _spawn_bg(
        _bg_run_ingest(
            conv_id=conv_id,
            tool_use_id=tool_use_id,
            new_run_id=new_run_id,
            project_id=db.project_id,
            url=url,
            target_short=target_short,
            headline=headline,
        )
    )
    return (
        f"Ingest started for {url[:80]}. Trace: /traces/{new_run_id}. "
        f"Running in background — completion will appear in chat."
    )


async def _bg_run_evaluate(
    *,
    conv_id: str,
    tool_use_id: str,
    new_run_id: str,
    project_id: str,
    question_id: str,
    headline: str,
    eval_type: str,
) -> None:
    """Background task: run an evaluation agent + persist completion."""
    from rumil.dispatch import dispatch_evaluation

    settings = get_settings()
    bg_db = await DB.create(
        run_id=new_run_id,
        prod=settings.is_prod_db,
        project_id=project_id,
    )
    trace_url = f"/traces/{new_run_id}"
    content: dict[str, Any]
    try:
        await bg_db.create_run(
            name=f"evaluate (chat, {eval_type}): {headline[:90]}",
            question_id=question_id,
            config=settings.capture_config(),
        )
        try:
            call = await dispatch_evaluation(question_id, bg_db, eval_type=eval_type)
            content = {
                "tool_use_id": tool_use_id,
                "run_id": new_run_id,
                "call_id": call.id,
                "call_type": "evaluate",
                "eval_type": eval_type,
                "question_id": question_id,
                "headline": headline,
                "status": "completed",
                "summary": (
                    f"Evaluation call {call.id[:8]} on '{headline[:40]}' "
                    f"completed (eval_type={eval_type})."
                ),
                "trace_url": trace_url,
            }
        except Exception as e:
            log.exception("Evaluation run %s failed", new_run_id[:8])
            content = {
                "tool_use_id": tool_use_id,
                "run_id": new_run_id,
                "call_type": "evaluate",
                "eval_type": eval_type,
                "question_id": question_id,
                "headline": headline,
                "status": "failed",
                "summary": f"Evaluation run {new_run_id[:8]} failed: {e}",
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


async def _run_evaluate(
    db: DB,
    params: dict[str, Any],
    on_progress: Callable[[str], Any] | None = None,
    *,
    conv_id: str | None = None,
    tool_use_id: str | None = None,
) -> str:
    """Fire-and-forget evaluation run."""
    question_id = params["question_id"]
    headline = params.get("headline", question_id[:8])
    eval_type = params.get("eval_type", "default")

    if not conv_id or not tool_use_id:
        return "Internal error: evaluate tool is missing conversation context."
    if not db.project_id:
        return "Internal error: evaluate tool has no project scope."

    new_run_id = str(uuid.uuid4())
    _spawn_bg(
        _bg_run_evaluate(
            conv_id=conv_id,
            tool_use_id=tool_use_id,
            new_run_id=new_run_id,
            project_id=db.project_id,
            question_id=question_id,
            headline=headline,
            eval_type=eval_type,
        )
    )
    return (
        f"Evaluation started on '{headline[:40]}' (eval_type={eval_type}). "
        f"Trace: /traces/{new_run_id}. Running in background — "
        f"completion will appear in chat."
    )


async def _bg_run_ground(
    *,
    conv_id: str,
    tool_use_id: str,
    new_run_id: str,
    project_id: str,
    question_id: str,
    evaluation_text: str,
    pipeline: str,
    from_stage: int,
    prior_checkpoints: Any,
) -> None:
    """Background task: run a grounding pipeline + persist completion."""
    from rumil.dispatch import dispatch_grounding_pipeline

    settings = get_settings()
    bg_db = await DB.create(
        run_id=new_run_id,
        prod=settings.is_prod_db,
        project_id=project_id,
    )
    trace_url = f"/traces/{new_run_id}"
    content: dict[str, Any]
    try:
        question = await bg_db.get_page(question_id)
        headline = question.headline if question else question_id[:8]
        await bg_db.create_run(
            name=f"{pipeline} (chat): {headline[:90]}",
            question_id=question_id,
            config=settings.capture_config(),
        )
        try:
            call = await dispatch_grounding_pipeline(
                pipeline,
                question_id,
                evaluation_text,
                bg_db,
                from_stage=from_stage,
                prior_checkpoints=prior_checkpoints,
            )
            content = {
                "tool_use_id": tool_use_id,
                "run_id": new_run_id,
                "call_id": call.id,
                "call_type": "ground",
                "pipeline": pipeline,
                "question_id": question_id,
                "headline": headline,
                "status": "completed",
                "summary": (
                    f"{pipeline} pipeline call {call.id[:8]} on '{headline[:40]}' completed."
                ),
                "trace_url": trace_url,
            }
        except Exception as e:
            log.exception("%s run %s failed", pipeline, new_run_id[:8])
            content = {
                "tool_use_id": tool_use_id,
                "run_id": new_run_id,
                "call_type": "ground",
                "pipeline": pipeline,
                "question_id": question_id,
                "headline": headline,
                "status": "failed",
                "summary": f"{pipeline} run {new_run_id[:8]} failed: {e}",
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


async def _run_ground(
    db: DB,
    params: dict[str, Any],
    on_progress: Callable[[str], Any] | None = None,
    *,
    conv_id: str | None = None,
    tool_use_id: str | None = None,
) -> str:
    """Fire-and-forget grounding pipeline run."""
    from rumil.models import CallType as _CallType

    call_short = params["eval_call_id"]
    pipeline = params.get("pipeline", "grounding")
    from_stage = params.get("from_stage", 1)

    if not conv_id or not tool_use_id:
        return "Internal error: ground_evaluation tool is missing conversation context."
    if not db.project_id:
        return "Internal error: ground_evaluation tool has no project scope."

    eval_full_id = await db.resolve_call_id(call_short)
    if not eval_full_id:
        return f"Evaluation call '{call_short}' not found."
    eval_call = await db.get_call(eval_full_id)
    if not eval_call:
        return f"Evaluation call '{call_short}' not found."
    if eval_call.call_type != _CallType.EVALUATE:
        return (
            f"Call {eval_call.id[:8]} is a {eval_call.call_type.value}, not "
            f"EVALUATE — cannot run grounding pipeline on it."
        )
    evaluation_text = (eval_call.review_json or {}).get("evaluation", "")
    if not evaluation_text:
        return (
            f"Evaluation call {eval_call.id[:8]} has no 'evaluation' field in "
            f"review_json (status={eval_call.status.value})."
        )
    question_id = eval_call.scope_page_id
    if not question_id:
        return f"Evaluation call {eval_call.id[:8]} has no scope_page_id."

    prior_checkpoints = (eval_call.call_params or {}).get("checkpoints") if from_stage > 1 else None

    new_run_id = str(uuid.uuid4())
    _spawn_bg(
        _bg_run_ground(
            conv_id=conv_id,
            tool_use_id=tool_use_id,
            new_run_id=new_run_id,
            project_id=db.project_id,
            question_id=question_id,
            evaluation_text=evaluation_text,
            pipeline=pipeline,
            from_stage=from_stage,
            prior_checkpoints=prior_checkpoints,
        )
    )
    return (
        f"{pipeline} pipeline started on eval {eval_call.id[:8]} from stage {from_stage}. "
        f"Trace: /traces/{new_run_id}. Running in background — "
        f"completion will appear in chat."
    )


_ASYNC_HANDLERS: dict[str, Callable[..., Any]] = {
    "__async_dispatch__": _run_dispatch,
    "__async_orchestrate__": _run_orchestrate,
    "__async_ingest__": _run_ingest,
    "__async_evaluate__": _run_evaluate,
    "__async_ground__": _run_ground,
}


async def _resolve_async(
    result_str: str,
    db: DB,
    on_progress: Callable[[str], Any] | None = None,
    *,
    conv_id: str | None = None,
    tool_use_id: str | None = None,
) -> str:
    """If result contains an async sentinel, run the handler. Otherwise pass through.

    ``conv_id`` and ``tool_use_id`` are plumbed through so handlers that
    fire-and-forget (currently only ``_run_dispatch``) can link a
    background-written ``DISPATCH_RESULT`` message back to its originating
    tool-use bubble. Handlers that still block ignore these kwargs.
    """
    for sentinel, handler in _ASYNC_HANDLERS.items():
        if f'"{sentinel}"' in result_str:
            params = json.loads(result_str)
            return await handler(
                db,
                params,
                on_progress=on_progress,
                conv_id=conv_id,
                tool_use_id=tool_use_id,
            )
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
    open_call_id: str | None = None,
    drawer_page_id: str | None = None,
    active_section: str | None = None,
    review_open: bool = False,
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

    if open_call_id:
        lines.append(f"- Selected call in trace: {open_call_id[:8]}")

    if drawer_page_id:
        try:
            resolved = await db.resolve_page_id(drawer_page_id)
            page = await db.get_page(resolved) if resolved else None
            if page:
                headline = (page.headline or "")[:80]
                lines.append(
                    f'- Open in inspect panel: {page.id[:8]} ({page.page_type.value}, "{headline}")'
                )
        except Exception:
            log.debug("Failed to resolve drawer_page_id '%s'", drawer_page_id, exc_info=True)

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
                lines.append("- Pinned panes:")
                lines.extend(rendered)
        except Exception:
            log.debug("Failed to resolve open_page_ids %r", list(open_page_ids), exc_info=True)

    if active_section:
        lines.append(f"- Reading section: {active_section}")

    if review_open:
        lines.append("- Suggestion review modal is open")

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


async def _persist_user_turn(
    db: DB,
    conv: ChatConversation,
    request: ChatRequest,
    question_full_id: str | None,
) -> None:
    """Persist the newest user message from the request (if not already persisted).

    Existing messages are addressed by count: if the DB already has N stored
    messages and the request carries >N messages, persist everything new
    (only the user-originated entries; assistant turns are persisted as they
    happen during generation).

    Each new user message is tagged with `question_full_id` — the research
    question this turn was asked against. Conversations can span multiple
    questions within a project, so the tag is per-message.
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
            question_id=question_full_id,
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
    """Handle a chat request: build context, call LLM with tools, return response.

    Wraps the turn body in ``asyncio.wait_for(..., CHAT_TURN_TIMEOUT_S)`` so a
    stuck round (hung upstream call, wedged tool) eventually returns an error
    response instead of hanging the request indefinitely. Mirrors the stream
    path's timeout behavior.
    """
    settings = get_settings()
    model_id = MODEL_MAP.get(request.model, MODEL_MAP["sonnet"])
    db = await DB.create(
        run_id=str(uuid.uuid4()),
        prod=settings.is_prod_db,
    )
    project, _ = await db.get_or_create_project(request.workspace)
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
    log.info(
        "chat-turn run=%s ws=%s qid=%s model_short=%s model=%s",
        db.run_id,
        request.workspace,
        request.question_id or "",
        request.model,
        model_id,
    )
    executor = RunExecutor(db)

    async def run_turn() -> ChatResponse:
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

        await _persist_user_turn(db, conv, request, full_id)

        system_prompt = (PROMPTS_DIR / "api_chat.md").read_text(encoding="utf-8")
        catalog = _build_call_type_catalog()
        orchestrator_catalog = _build_orchestrator_catalog()
        context_scope_id = full_id or ""
        context_text = await build_chat_context(full_id, db) if full_id else "(no question scope)"
        ui_block = await _build_ui_state_block(
            db,
            request.open_run_id,
            request.open_page_ids,
            request.view_mode,
            request.open_call_id,
            request.drawer_page_id,
            request.active_section,
            request.review_open,
        )
        preamble = f"{ui_block}\n\n" if ui_block else ""
        full_system = (
            f"{system_prompt}\n\n{catalog}\n\n{orchestrator_catalog}"
            f"\n\n---\n\n{preamble}{context_text}"
        )

        client = make_anthropic_client()
        tool_uses_log: list[ToolUseInfo] = []

        turn_start_iso = datetime.now(UTC).isoformat()
        turn_chat_usd = 0.0

        for _ in range(10):
            create_kwargs: dict[str, Any] = {
                "model": model_id,
                "max_tokens": 4096,
                "temperature": 0.7,
                "system": full_system,
                "messages": messages,
                "tools": TOOLS,
            }
            exchange_started = datetime.now(UTC)
            try:
                response = await client.messages.create(**create_kwargs)  # type: ignore[arg-type]
            except Exception as exc:
                await llm_boundary.log_exchange(
                    source="chat.handle_chat",
                    model=model_id,
                    request_payload=create_kwargs,
                    started_at=exchange_started,
                    finished_at=datetime.now(UTC),
                    error=exc,
                )
                raise
            await llm_boundary.log_exchange(
                source="chat.handle_chat",
                model=model_id,
                request_payload=create_kwargs,
                started_at=exchange_started,
                finished_at=datetime.now(UTC),
                response=response,
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
                question_id=full_id,
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
                result_str = await _execute_tool_timed(tc.name, tc.input, db, context_scope_id)
                result_str = await _resolve_async(
                    result_str, db, conv_id=conv.id, tool_use_id=tc.id
                )
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
                question_id=full_id,
            )

            messages.append({"role": "user", "content": tool_results})

        await db.update_chat_conversation(conv.id, touch=True)
        return ChatResponse(
            response="Reached maximum tool-use rounds.",
            tool_uses=tool_uses_log,
            conversation_id=conv.id,
        )

    try:
        try:
            async with executor.tracked_scope(db.run_id):
                return await asyncio.wait_for(run_turn(), timeout=CHAT_TURN_TIMEOUT_S)
        except TimeoutError:
            log.warning(
                "chat turn hit CHAT_TURN_TIMEOUT_S (%ss); abandoning",
                CHAT_TURN_TIMEOUT_S,
            )
            return ChatResponse(
                response=f"Chat turn exceeded {CHAT_TURN_TIMEOUT_S:.0f}s and was abandoned.",
                tool_uses=[],
                conversation_id=request.conversation_id or "",
            )
        except Exception as e:
            log.error("Chat error: %s", e, exc_info=True)
            return ChatResponse(
                response=f"Chat error: {e}",
                tool_uses=[],
                conversation_id=request.conversation_id or "",
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
        elif m.role == ChatMessageRole.DISPATCH_RESULT:
            content = m.content if isinstance(m.content, dict) else {}
            status = content.get("status", "completed")
            summary = content.get("summary", "")
            run_short = (content.get("run_id") or "")[:8]
            trace_url = content.get("trace_url", "")
            text = f"[dispatch completed] run {run_short} {status}: {summary} ({trace_url})"
            out.append({"role": "user", "content": text})
    return out


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


async def handle_conversation_events(conversation_id: str) -> StreamingResponse:
    """Long-lived SSE stream of out-of-band events for a conversation.

    Events include ``dispatch_completed`` (fire-and-forget research-call
    completions written by a background task) and, in later phases, other
    per-conversation signals like orchestrator progress. Unlike
    ``/api/chat/stream`` this stream has no turn lifetime — it stays open
    as long as the client holds it. Used for rendering completion chips
    on tool bubbles that finished after the triggering turn ended.
    """
    q = _subscribe_conv(conversation_id)

    async def generate() -> AsyncIterator[str]:
        try:
            yield _sse("hello", {"conversation_id": conversation_id})
            while True:
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield _sse(payload["event"], payload["data"])
                except TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            _unsubscribe_conv(conversation_id, q)

    return StreamingResponse(generate(), media_type="text/event-stream")


async def handle_chat_stream(request: ChatRequest) -> StreamingResponse:
    """Handle a streaming chat request, yielding SSE events."""
    settings = get_settings()
    model_id = MODEL_MAP.get(request.model, MODEL_MAP["sonnet"])
    db = await DB.create(
        run_id=str(uuid.uuid4()),
        prod=settings.is_prod_db,
    )
    project, _ = await db.get_or_create_project(request.workspace)
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
    log.info(
        "chat-turn run=%s ws=%s qid=%s model_short=%s model=%s stream=1",
        db.run_id,
        request.workspace,
        request.question_id or "",
        request.model,
        model_id,
    )
    executor = RunExecutor(db)

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

    await _persist_user_turn(db, conv, request, full_id)

    system_prompt = (PROMPTS_DIR / "api_chat.md").read_text(encoding="utf-8")
    catalog = _build_call_type_catalog()
    orchestrator_catalog = _build_orchestrator_catalog()
    context_text = await build_chat_context(full_id, db) if full_id else "(no question scope)"
    ui_block = await _build_ui_state_block(
        db,
        request.open_run_id,
        request.open_page_ids,
        request.view_mode,
        request.open_call_id,
        request.drawer_page_id,
        request.active_section,
        request.review_open,
    )
    preamble = f"{ui_block}\n\n" if ui_block else ""
    full_system = f"{system_prompt}\n\n{catalog}\n\n---\n\n{preamble}{context_text}"
    client = make_anthropic_client()
    context_scope_id = full_id or ""

    event_q: asyncio.Queue[str | None] = asyncio.Queue()

    async def run_turn() -> None:
        """Drive the Anthropic turn to completion and persist results.

        Emits SSE-encoded event strings onto `event_q`. Runs to completion
        even if the browser disconnects — this coroutine is scheduled as a
        detached asyncio.Task, so CancelledError from the SSE generator
        does not propagate here. The only things that cancel us are the
        outer wait_for timeout (CHAT_TURN_TIMEOUT_S) and process shutdown.
        On exit (success, error, or timeout) pushes a sentinel (None) so
        the reader can stop.
        """
        nonlocal messages
        turn_start_iso = datetime.now(UTC).isoformat()
        turn_chat_usd = 0.0
        try:
            async with executor.tracked_scope(db.run_id):
                event_q.put_nowait(
                    _sse("conversation", {"conversation_id": conv.id, "title": conv.title})
                )
                for _ in range(10):
                    stream_kwargs: dict[str, Any] = {
                        "model": model_id,
                        "max_tokens": 4096,
                        "temperature": 0.7,
                        "system": full_system,
                        "messages": messages,
                        "tools": TOOLS,
                    }
                    exchange_started = datetime.now(UTC)
                    try:
                        async with client.messages.stream(**stream_kwargs) as stream:  # type: ignore[arg-type]
                            async for event in stream:
                                if event.type == "content_block_delta":
                                    if isinstance(event.delta, TextDelta):
                                        event_q.put_nowait(
                                            _sse("text", {"content": event.delta.text})
                                        )
                                elif event.type == "content_block_start":
                                    if isinstance(event.content_block, ToolUseBlock):
                                        event_q.put_nowait(
                                            _sse(
                                                "tool_use_start",
                                                {
                                                    "id": event.content_block.id,
                                                    "name": event.content_block.name,
                                                },
                                            )
                                        )

                        response = await stream.get_final_message()
                    except Exception as exc:
                        await llm_boundary.log_exchange(
                            source="chat.handle_chat_stream",
                            model=model_id,
                            request_payload=stream_kwargs,
                            started_at=exchange_started,
                            finished_at=datetime.now(UTC),
                            error=exc,
                            streamed=True,
                        )
                        raise
                    await llm_boundary.log_exchange(
                        source="chat.handle_chat_stream",
                        model=model_id,
                        request_payload=stream_kwargs,
                        started_at=exchange_started,
                        finished_at=datetime.now(UTC),
                        response=response,
                        streamed=True,
                    )
                    try:
                        turn_chat_usd += usd_from_usage(model_id, response.usage)
                    except KeyError:
                        log.warning(
                            "No pricing entry for model %s; chat cost not counted", model_id
                        )

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
                        question_id=full_id,
                    )

                    if is_terminal and costs_payload is not None:
                        event_q.put_nowait(_sse("turn_costs", costs_payload))
                        break

                    messages.append({"role": "assistant", "content": response.content})

                    tool_results = []
                    for tc in tool_calls:
                        result_str = await _execute_tool_timed(
                            tc.name, tc.input, db, context_scope_id
                        )
                        has_async = any(f'"{s}"' in result_str for s in _ASYNC_HANDLERS)
                        if has_async:
                            progress_q: asyncio.Queue[str] = asyncio.Queue()
                            task = asyncio.create_task(
                                _resolve_async(
                                    result_str,
                                    db,
                                    on_progress=lambda m: progress_q.put_nowait(m),
                                    conv_id=conv.id,
                                    tool_use_id=tc.id,
                                )
                            )
                            while not task.done():
                                try:
                                    msg = await asyncio.wait_for(progress_q.get(), timeout=0.5)
                                    event_q.put_nowait(
                                        _sse("orchestrator_progress", {"message": msg})
                                    )
                                except TimeoutError:
                                    continue
                            result_str = await task
                            while not progress_q.empty():
                                event_q.put_nowait(
                                    _sse(
                                        "orchestrator_progress",
                                        {"message": progress_q.get_nowait()},
                                    )
                                )
                        event_q.put_nowait(
                            _sse(
                                "tool_use_result",
                                {
                                    "id": tc.id,
                                    "name": tc.name,
                                    "input": tc.input,
                                    "result": result_str[:500],
                                },
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
                        question_id=full_id,
                    )

                    messages.append({"role": "user", "content": tool_results})

                await db.update_chat_conversation(conv.id, touch=True)
                event_q.put_nowait(_sse("done", {"conversation_id": conv.id}))
        except asyncio.CancelledError:
            # This fires when the outer wait_for hits CHAT_TURN_TIMEOUT_S.
            # The client disconnect path does NOT cancel us (we're a
            # detached task). Partial state already persisted to DB stays.
            log.warning(
                "chat turn hit CHAT_TURN_TIMEOUT_S (%ss); abandoning (conv=%s)",
                CHAT_TURN_TIMEOUT_S,
                conv.id,
            )
            raise
        except Exception as e:
            log.error("Chat stream error: %s", e, exc_info=True)
            event_q.put_nowait(_sse("error", {"message": str(e)}))
        finally:
            event_q.put_nowait(None)
            await db.close()

    turn_task: asyncio.Task[None] = asyncio.create_task(
        asyncio.wait_for(run_turn(), timeout=CHAT_TURN_TIMEOUT_S)
    )
    _live_chat_turns.add(turn_task)
    turn_task.add_done_callback(_live_chat_turns.discard)

    async def generate() -> AsyncIterator[str]:
        """Drain events from the detached turn task until it signals done.

        `turn_task` is created with asyncio.create_task and registered in a
        module-level strong-ref set, making its lifetime fully independent
        of this generator. When the browser disconnects, FastAPI cancels
        the generator — but that does not propagate to turn_task. The task
        continues running server-side: it finishes the Anthropic stream
        and persists the assistant message + tool_results. On the next
        browser load, the completed turn shows up via the normal
        conversation-load path.
        """
        try:
            while True:
                evt = await event_q.get()
                if evt is None:
                    break
                yield evt
        except asyncio.CancelledError:
            log.info(
                "chat SSE generator cancelled by client; turn continues in background (conv=%s)",
                conv.id,
            )
            raise

    return StreamingResponse(generate(), media_type="text/event-stream")
