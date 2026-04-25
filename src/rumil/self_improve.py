"""
Self-improvement analysis: an LLM reviews a completed investigation
against rumil's own source code and produces a markdown narrative of
strengths, weaknesses, and suggested code/prompt improvements.

One-off utility like --summary: not traced, not budget-counted. The
final LLM message is saved to pages/self-improvement/.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import anthropic
from anthropic.types import TextBlock, ToolUseBlock

from rumil.calls.common import execute_tool_uses, prepare_tools
from rumil.database import DB, _row_to_call
from rumil.llm import Tool, call_api
from rumil.models import Call, Page, PageType
from rumil.prompts import PROMPTS_DIR
from rumil.settings import get_settings

log = logging.getLogger(__name__)

REPO_ROOT = (Path(__file__).parent.parent.parent).resolve()
OUTPUT_DIR = Path(__file__).parent.parent.parent / "pages" / "self-improvement"

MAX_AGENT_ROUNDS = 60
MAX_FILE_CHARS = 80_000
MAX_EXCHANGE_BLOB_CHARS = 120_000

IGNORED_DIR_NAMES = {
    ".git",
    "__pycache__",
    "node_modules",
    ".next",
    ".venv",
    "venv",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    "pages",
    "reports",
}


def _load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n[... truncated, {len(text) - limit} more chars ...]"


_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _is_full_uuid(s: str) -> bool:
    return bool(_UUID_RE.match(s))


def _safe_repo_path(rel_path: str) -> Path | None:
    if not rel_path or rel_path.startswith("/"):
        return None
    candidate = (REPO_ROOT / rel_path).resolve()
    try:
        candidate.relative_to(REPO_ROOT)
    except ValueError:
        return None
    return candidate


async def _collect_subtree(
    root_id: str,
    db: DB,
    max_depth: int = 12,
) -> Sequence[tuple[Page, int]]:
    """BFS through child questions. Cycle-safe (tracks visited)."""
    visited: set[str] = set()
    result: list[tuple[Page, int]] = []
    root = await db.get_page(root_id)
    if not root:
        return result
    frontier: list[tuple[Page, int]] = [(root, 0)]
    while frontier:
        next_frontier: list[tuple[Page, int]] = []
        for page, depth in frontier:
            if page.id in visited:
                continue
            visited.add(page.id)
            result.append((page, depth))
            if depth < max_depth:
                children = await db.get_child_questions(page.id)
                for child in children:
                    if child.id not in visited:
                        next_frontier.append((child, depth + 1))
        frontier = next_frontier
    return result


async def _fetch_subtree_calls(
    subtree_ids: Sequence[str],
    db: DB,
) -> Sequence[Call]:
    """Fetch all calls whose scope_page_id is in the subtree."""
    if not subtree_ids:
        return []
    rows = await db._execute(
        db.client.table("calls")
        .select("*")
        .in_("scope_page_id", list(subtree_ids))
        .order("created_at")
    )
    return [_row_to_call(r) for r in (rows.data or [])]


def _format_call_summary(c: Call) -> str:
    scope = c.scope_page_id[:8] if c.scope_page_id else "none"
    cost = f"${c.cost_usd:.3f}" if c.cost_usd is not None else "?"
    return (
        f"[{c.id[:8]}] {c.call_type.value}  scope={scope}  "
        f"status={c.status.value}  cost={cost}  budget_used={c.budget_used}"
    )


def _build_tools(
    root_question_id: str,
    subtree: Sequence[tuple[Page, int]],
    calls: Sequence[Call],
    db: DB,
) -> Sequence[Tool]:
    call_ids = {c.id for c in calls}

    async def get_investigation_overview(inp: dict) -> str:
        parts: list[str] = []
        root = next((p for p, d in subtree if d == 0), None)
        if root:
            parts.append(f"# Root question [{root.id[:8]}]: {root.headline}")
            if root.content:
                parts.append(_truncate(root.content, 2000))
            parts.append("")

        parts.append(f"## Subtree ({len(subtree)} questions)")
        for p, depth in subtree:
            indent = "  " * depth
            parts.append(f"{indent}- [{p.id[:8]}] {p.headline}")
        parts.append("")

        parts.append(f"## Calls in subtree ({len(calls)})")
        for c in calls:
            parts.append(f"- {_format_call_summary(c)}")
        if not calls:
            parts.append("(none)")
        parts.append("")

        page_counts: dict[str, int] = {}
        total_pages_by_call = 0
        if call_ids:
            pages_rows = await db._execute(
                db.client.table("pages")
                .select("id, page_type")
                .in_("provenance_call_id", list(call_ids))
            )
            for r in pages_rows.data or []:
                pt = r["page_type"]
                page_counts[pt] = page_counts.get(pt, 0) + 1
                total_pages_by_call += 1

        parts.append(f"## Pages created by these calls ({total_pages_by_call})")
        for pt, n in sorted(page_counts.items(), key=lambda kv: -kv[1]):
            parts.append(f"- {pt}: {n}")
        return "\n".join(parts)

    async def read_page(inp: dict) -> str:
        page_id = inp.get("page_id", "")
        resolved = await db.resolve_page_id(page_id)
        if not resolved:
            return f"Page '{page_id}' not found."
        page = await db.get_page(resolved)
        if not page:
            return f"Page '{page_id}' not found."
        lines = [
            f"ID: {page.id}",
            f"Type: {page.page_type.value}",
            f"Headline: {page.headline}",
        ]
        if page.abstract:
            lines.append(f"Abstract: {page.abstract}")
        if page.credence is not None:
            lines.append(f"Credence: {page.credence}")
        if page.robustness is not None:
            lines.append(f"Robustness: {page.robustness}")
        if page.is_superseded:
            lines.append(f"Superseded by: {page.superseded_by}")
        if page.provenance_call_id:
            lines.append(
                f"Provenance: {page.provenance_call_type} "
                f"call {page.provenance_call_id[:8]} "
                f"(model: {page.provenance_model})"
            )
        lines.append("")
        lines.append(_truncate(page.content or "", 20_000))
        if page.extra:
            lines.append("")
            lines.append("Extra:")
            lines.append(_truncate(json.dumps(page.extra, indent=2, default=str), 4000))
        return "\n".join(lines)

    async def list_pages_for_call(inp: dict) -> str:
        call_id_arg = inp.get("call_id", "")
        resolved = await db.resolve_call_id(call_id_arg) or call_id_arg
        rows = await db._execute(
            db.client.table("pages")
            .select("id, page_type, headline, credence, robustness, is_superseded")
            .eq("provenance_call_id", resolved)
            .order("created_at")
        )
        data = rows.data or []
        if not data:
            return f"No pages created by call {resolved[:8]}."
        lines = [f"{len(data)} page(s) created by call {resolved[:8]}:"]
        for r in data:
            bits = [f"[{r['id'][:8]}]", r["page_type"]]
            if r.get("credence") is not None:
                bits.append(f"C{r['credence']}")
            if r.get("robustness") is not None:
                bits.append(f"R{r['robustness']}")
            if r.get("is_superseded"):
                bits.append("(superseded)")
            lines.append(f"- {' '.join(bits)}  {r['headline']}")
        return "\n".join(lines)

    async def get_call_details(inp: dict) -> str:
        call_id_arg = inp.get("call_id", "")
        resolved = await db.resolve_call_id(call_id_arg)
        if not resolved:
            return f"Call '{call_id_arg}' not found."
        call = await db.get_call(resolved)
        if not call:
            return f"Call '{call_id_arg}' not found."

        lines = [
            f"ID: {call.id}",
            f"Type: {call.call_type.value}",
            f"Status: {call.status.value}",
            f"Scope: {call.scope_page_id or '(none)'}",
            f"Parent call: {call.parent_call_id or '(none)'}",
            f"Budget used/allocated: {call.budget_used}/{call.budget_allocated}",
            f"Cost: ${call.cost_usd:.4f}" if call.cost_usd is not None else "Cost: ?",
            f"Created: {call.created_at.isoformat()}",
        ]
        if call.completed_at:
            lines.append(f"Completed: {call.completed_at.isoformat()}")
        if call.result_summary:
            lines.append("")
            lines.append("## Result summary")
            lines.append(_truncate(call.result_summary, 10_000))
        if call.review_json:
            lines.append("")
            lines.append("## Review JSON")
            lines.append(_truncate(json.dumps(call.review_json, indent=2, default=str), 10_000))
        if call.call_params:
            lines.append("")
            lines.append("## Call params")
            lines.append(_truncate(json.dumps(call.call_params, indent=2, default=str), 4000))

        exchanges = await db.get_llm_exchanges(resolved)
        lines.append("")
        lines.append(f"## LLM exchanges ({len(exchanges)})")
        lines.append(
            "(pass the full exchange id to get_llm_exchange — short prefixes aren't accepted here)"
        )
        for e in exchanges:
            lines.append(
                f"- {e['id']} phase={e.get('phase')} "
                f"round={e.get('round')} "
                f"in={e.get('input_tokens')} out={e.get('output_tokens')} "
                f"ms={e.get('duration_ms')}"
            )

        trace = await db.get_call_trace(resolved)
        lines.append("")
        lines.append(f"## Trace events ({len(trace)})")
        for ev in trace:
            etype = ev.get("event", "?")
            phase = ev.get("phase", "")
            lines.append(f"- {etype} {phase}".rstrip())
        return _truncate("\n".join(lines), 40_000)

    async def get_llm_exchange(inp: dict) -> str:
        exchange_id = inp.get("exchange_id", "").strip()
        if not exchange_id:
            return "Error: exchange_id required."
        if not _is_full_uuid(exchange_id):
            return (
                f"Error: '{exchange_id}' is not a full UUID. Call "
                "get_call_details(call_id) and copy the full exchange id "
                "from the listing — short prefixes aren't accepted for "
                "exchanges."
            )
        exchange = await db.get_llm_exchange(exchange_id)
        if not exchange:
            return f"Exchange '{exchange_id}' not found."

        lines = [
            f"ID: {exchange['id']}",
            f"Call: {exchange.get('call_id')}",
            f"Phase: {exchange.get('phase')}",
            f"Round: {exchange.get('round')}",
        ]
        system_prompt = exchange.get("system_prompt") or ""
        user_message = exchange.get("user_message") or ""
        user_messages = exchange.get("user_messages") or []
        response_text = exchange.get("response_text") or ""
        tool_calls = exchange.get("tool_calls") or []

        lines.append("")
        lines.append("## System prompt")
        lines.append(_truncate(system_prompt, 20_000))
        lines.append("")
        lines.append("## User message(s)")
        if user_messages:
            lines.append(_truncate(json.dumps(user_messages, indent=2, default=str), 40_000))
        else:
            lines.append(_truncate(user_message, 20_000))
        lines.append("")
        lines.append("## Response text")
        lines.append(_truncate(response_text, 20_000))
        if tool_calls:
            lines.append("")
            lines.append("## Tool calls")
            lines.append(_truncate(json.dumps(tool_calls, indent=2, default=str), 20_000))
        return _truncate("\n".join(lines), MAX_EXCHANGE_BLOB_CHARS)

    async def read_repo_file(inp: dict) -> str:
        rel = inp.get("path", "")
        path = _safe_repo_path(rel)
        if path is None:
            return f"Error: path '{rel}' is outside the rumil repo or invalid."
        if not path.exists():
            return f"Error: file '{rel}' does not exist."
        if not path.is_file():
            return f"Error: '{rel}' is not a file."
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return f"Error: file '{rel}' is not a text file."
        return _truncate(text, MAX_FILE_CHARS)

    async def list_repo_dir(inp: dict) -> str:
        rel = inp.get("path", "").strip() or "."
        path = _safe_repo_path(rel)
        if path is None:
            return f"Error: path '{rel}' is outside the rumil repo or invalid."
        if not path.exists():
            return f"Error: directory '{rel}' does not exist."
        if not path.is_dir():
            return f"Error: '{rel}' is not a directory."
        entries = []
        for child in sorted(path.iterdir()):
            if child.name.startswith(".") and child.name not in {".env.example"}:
                continue
            if child.is_dir() and child.name in IGNORED_DIR_NAMES:
                continue
            suffix = "/" if child.is_dir() else ""
            entries.append(f"{child.name}{suffix}")
        if not entries:
            return f"(directory '{rel}' is empty or all entries filtered)"
        return "\n".join(entries)

    return [
        Tool(
            name="get_investigation_overview",
            description=(
                "Return a structured overview of the investigation: the root "
                "question, the full subtree of sub-questions, every call that "
                "ran against any question in the subtree, and counts of pages "
                "those calls produced. Start here to orient yourself."
            ),
            input_schema={"type": "object", "properties": {}},
            fn=get_investigation_overview,
        ),
        Tool(
            name="read_page",
            description=(
                "Read the full content of any page (claim, question, "
                "judgement, concept, view, view item, source, wiki) by its "
                "id (short 8-char prefix or full UUID). Returns headline, "
                "abstract, content, credence/robustness, provenance, extras."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "page_id": {"type": "string", "description": "Page id."},
                },
                "required": ["page_id"],
            },
            fn=read_page,
        ),
        Tool(
            name="list_pages_for_call",
            description=(
                "List the pages a specific call created (via "
                "provenance_call_id). Short headlines + ids — use read_page "
                "to dig into any one."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "call_id": {"type": "string", "description": "Call id."},
                },
                "required": ["call_id"],
            },
            fn=list_pages_for_call,
        ),
        Tool(
            name="get_call_details",
            description=(
                "Read a call's metadata: type, status, scope, budget, cost, "
                "result_summary, review_json, call_params, list of LLM "
                "exchange ids, and a summary of trace events. Use "
                "get_llm_exchange for the verbatim prompt/response of any "
                "exchange."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "call_id": {"type": "string", "description": "Call id."},
                },
                "required": ["call_id"],
            },
            fn=get_call_details,
        ),
        Tool(
            name="get_llm_exchange",
            description=(
                "Read a single LLM exchange verbatim: system prompt, user "
                "message(s), response text, and tool calls. Large — only "
                "fetch one at a time when you want to see exactly what the "
                "model saw and said. Accepts short id prefix."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "exchange_id": {
                        "type": "string",
                        "description": "LLM exchange id (short prefix ok).",
                    },
                },
                "required": ["exchange_id"],
            },
            fn=get_llm_exchange,
        ),
        Tool(
            name="read_repo_file",
            description=(
                "Read a file from the rumil repository — Python source, "
                "prompt files, migrations, configs, README, anything. Path "
                "is relative to the repo root (e.g. "
                "'prompts/find_considerations.md', "
                "'src/rumil/orchestrators/two_phase.py'). Read-only. Use "
                "this to inspect the actual code or prompts you think might "
                "be worth changing before suggesting edits."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Repo-relative path.",
                    },
                },
                "required": ["path"],
            },
            fn=read_repo_file,
        ),
        Tool(
            name="list_repo_dir",
            description=(
                "List entries in a directory of the rumil repo. Path is "
                "relative to repo root; defaults to '.'. Use this to "
                "discover files before reading them."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Repo-relative directory path.",
                    },
                },
            },
            fn=list_repo_dir,
        ),
    ]


async def _run_agent(
    system_prompt: str,
    user_message: str,
    tools: Sequence[Tool],
) -> str:
    settings = get_settings()
    client = anthropic.AsyncAnthropic(api_key=settings.require_anthropic_key())
    model = settings.model
    tool_defs, tool_fns = prepare_tools(list(tools))

    messages: list[dict] = [{"role": "user", "content": user_message}]
    final_text = ""

    for round_num in range(MAX_AGENT_ROUNDS):
        api_resp = await call_api(
            client,
            model,
            system_prompt,
            messages,
            tools=tool_defs,
            cache=True,
        )
        response = api_resp.message
        text_parts: list[str] = []
        tool_uses: list[ToolUseBlock] = []
        for block in response.content:
            if isinstance(block, TextBlock):
                text_parts.append(block.text)
            elif isinstance(block, ToolUseBlock):
                tool_uses.append(block)

        if tool_uses:
            _, tool_results = await execute_tool_uses(tool_uses, tool_fns)
            assistant_content = [b.model_dump() for b in response.content]
            messages.append({"role": "assistant", "content": assistant_content})
            messages.append({"role": "user", "content": tool_results})
            if text_parts:
                preview = " | ".join(t[:120] for t in text_parts)
                print(f"  [round {round_num + 1}] {len(tool_uses)} tool call(s): {preview}")
            else:
                print(f"  [round {round_num + 1}] {len(tool_uses)} tool call(s)")
            if response.stop_reason == "end_turn":
                final_text = "\n".join(text_parts)
                break
            continue

        final_text = "\n".join(text_parts)
        break
    else:
        log.warning(
            "Self-improve hit max rounds (%d) without a final text response",
            MAX_AGENT_ROUNDS,
        )
    return final_text


async def run_self_improvement(question_id: str, db: DB) -> str:
    """Run a self-improvement analysis for a question. Returns markdown text."""
    resolved = await db.resolve_page_id(question_id)
    if not resolved:
        raise ValueError(f"Question '{question_id}' not found")
    question = await db.get_page(resolved)
    if not question:
        raise ValueError(f"Question '{question_id}' not found")
    if question.page_type != PageType.QUESTION:
        raise ValueError(f"Page '{question_id}' is a {question.page_type.value}, not a question")

    if question.project_id and question.project_id != db.project_id:
        db.project_id = question.project_id

    subtree = await _collect_subtree(resolved, db)
    subtree_ids = [p.id for p, _ in subtree]
    calls = await _fetch_subtree_calls(subtree_ids, db)

    print(f"Collected subtree: {len(subtree)} question(s), {len(calls)} call(s).")

    tools = _build_tools(resolved, subtree, calls, db)
    system_prompt = _load_prompt("self_improve.md")
    user_message = (
        f"The investigation to analyse is rooted at question "
        f"[{resolved[:8]}]: {question.headline}\n\n"
        "Start by calling get_investigation_overview. Then drill in "
        "wherever the shape suggests something worth a closer look. "
        "When you're ready, write the full self-improvement analysis "
        "as your final message — that text is what gets saved to disk."
    )

    return await _run_agent(system_prompt, user_message, tools)


def save_self_improvement(text: str, question_headline: str) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    slug = "".join(c if c.isalnum() or c in " -" else "" for c in question_headline[:50])
    slug = slug.strip().replace(" ", "-").lower()
    filename = f"{timestamp}-{slug}.md"
    path = OUTPUT_DIR / filename
    path.write_text(text, encoding="utf-8")
    return path
