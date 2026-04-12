# /// script
# dependencies = ["fastapi", "uvicorn", "anthropic"]
# ///
"""Lightweight worldview API server.

SQLite-backed, no external dependencies beyond the Anthropic API.
Run with: uv run public-ui/serve.py

Data model:
  - workspaces: named containers
  - nodes: tree-structured worldview nodes (claim/hypothesis/evidence/uncertainty/context)
  - runs: agent action records (what the model did)
"""

import json
import os
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anthropic
import uvicorn
from anthropic.types import TextBlock, ToolUseBlock
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

DB_PATH = Path(__file__).parent / "worldview.db"
PROMPTS_DIR = Path(__file__).parent.parent / "prompts"

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS workspaces (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS nodes (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(id),
            parent_id TEXT REFERENCES nodes(id),
            node_type TEXT NOT NULL CHECK(node_type IN ('claim','hypothesis','evidence','uncertainty','context','question')),
            headline TEXT NOT NULL,
            content TEXT NOT NULL DEFAULT '',
            credence INTEGER CHECK(credence BETWEEN 1 AND 9),
            robustness INTEGER CHECK(robustness BETWEEN 1 AND 5),
            importance INTEGER NOT NULL DEFAULT 0,
            position INTEGER NOT NULL DEFAULT 0,
            source_ids TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            created_by TEXT NOT NULL DEFAULT 'system'
        );

        CREATE TABLE IF NOT EXISTS suggestions (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(id),
            run_id TEXT REFERENCES runs(id),
            suggestion_type TEXT NOT NULL,
            target_node_id TEXT REFERENCES nodes(id),
            payload TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'pending',
            reviewed_at TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(id),
            run_type TEXT NOT NULL DEFAULT 'chat',
            scope_node_id TEXT REFERENCES nodes(id),
            started_at TEXT NOT NULL,
            completed_at TEXT,
            status TEXT NOT NULL DEFAULT 'running',
            description TEXT,
            config TEXT NOT NULL DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS actions (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES runs(id),
            action_type TEXT NOT NULL,
            input_data TEXT,
            output_data TEXT,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_nodes_workspace ON nodes(workspace_id);
        CREATE INDEX IF NOT EXISTS idx_nodes_parent ON nodes(parent_id);
        CREATE INDEX IF NOT EXISTS idx_nodes_importance ON nodes(workspace_id, importance);
        CREATE INDEX IF NOT EXISTS idx_runs_workspace ON runs(workspace_id);
        CREATE INDEX IF NOT EXISTS idx_actions_run ON actions(run_id);
        CREATE INDEX IF NOT EXISTS idx_suggestions_workspace ON suggestions(workspace_id, status);
    """)
    conn.commit()
    conn.close()


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def new_id() -> str:
    return uuid.uuid4().hex[:16]



def _seed_nodes_recursive(
    conn: sqlite3.Connection,
    ws_id: str,
    parent_id: str,
    nodes_data: list[dict],
    depth: int = 0,
) -> None:
    for pos, node in enumerate(nodes_data):
        node_id = new_id()
        importance = min(depth, 4)
        conn.execute(
            "INSERT INTO nodes (id, workspace_id, parent_id, node_type, headline, content, "
            "credence, robustness, importance, position, source_ids, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                node_id, ws_id, parent_id,
                node.get("node_type", "claim"),
                node.get("headline", ""),
                node.get("content", ""),
                node.get("credence"),
                node.get("robustness"),
                importance,
                pos,
                json.dumps(node.get("source_page_ids", [])),
                now_iso(),
            ),
        )
        children = node.get("children", [])
        if children:
            _seed_nodes_recursive(conn, ws_id, node_id, children, depth + 1)


def seed_from_python_mock() -> None:
    """Seed using a Python-native mock (avoids parsing TS)."""
    conn = get_db()
    if conn.execute("SELECT COUNT(*) FROM workspaces").fetchone()[0] > 0:
        conn.close()
        return

    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    try:
        from _mock_data import MOCK_NODES, MOCK_HEADLINE, MOCK_SUMMARY  # type: ignore[import-not-found]
    except ImportError:
        conn.close()
        return
    finally:
        sys.path.pop(0)

    ws_id = new_id()
    conn.execute(
        "INSERT INTO workspaces (id, name, created_at) VALUES (?, ?, ?)",
        (ws_id, "default", now_iso()),
    )

    root_id = new_id()
    conn.execute(
        "INSERT INTO nodes (id, workspace_id, parent_id, node_type, headline, content, position, created_at) "
        "VALUES (?, ?, NULL, 'context', ?, ?, 0, ?)",
        (root_id, ws_id, MOCK_HEADLINE, MOCK_SUMMARY, now_iso()),
    )

    _seed_nodes_recursive(conn, ws_id, root_id, MOCK_NODES)
    conn.commit()
    conn.close()
    count = get_db().execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    print(f"  seeded workspace 'default': {count} nodes")


def get_subtree(conn: sqlite3.Connection, node_id: str) -> dict:
    """Load a node and its descendants as a nested dict."""
    row = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
    if not row:
        return {}
    node = dict(row)
    children = conn.execute(
        "SELECT * FROM nodes WHERE parent_id = ? ORDER BY position",
        (node_id,),
    ).fetchall()
    node["children"] = [get_subtree(conn, dict(c)["id"]) for c in children]
    return node


def format_tree(node: dict, depth: int = 0) -> str:
    """Render a node tree as readable text for context injection."""
    indent = "  " * depth
    parts = []
    ntype = node.get("node_type", "?")
    headline = node.get("headline", "?")
    nid = node.get("id", "?")[:8]
    cred = node.get("credence")
    rob = node.get("robustness")
    scores = ""
    if cred is not None:
        scores += f" C{cred}"
    if rob is not None:
        scores += f"/R{rob}"

    parts.append(f"{indent}[{ntype}] {headline} [{nid}]{scores}")

    content = node.get("content", "")
    if content and depth < 3:
        for line in content.split("\n"):
            parts.append(f"{indent}  {line}")

    for child in node.get("children", []):
        parts.append(format_tree(child, depth + 1))
    return "\n".join(parts)


def search_nodes(conn: sqlite3.Connection, ws_id: str, query: str, limit: int = 8) -> list[dict]:
    """Simple text search over nodes."""
    rows = conn.execute(
        "SELECT * FROM nodes WHERE workspace_id = ? "
        "AND (headline LIKE ? OR content LIKE ?) "
        "ORDER BY position LIMIT ?",
        (ws_id, f"%{query}%", f"%{query}%", limit),
    ).fetchall()
    return [dict(r) for r in rows]


def get_ancestors(conn: sqlite3.Connection, node_id: str) -> list[dict]:
    """Walk up parent_id chain to root. Returns [immediate_parent, ..., root]."""
    ancestors: list[dict] = []
    current_id = node_id
    visited: set[str] = set()
    while current_id:
        if current_id in visited:
            break
        visited.add(current_id)
        row = conn.execute("SELECT * FROM nodes WHERE id = ?", (current_id,)).fetchone()
        if not row:
            break
        node = dict(row)
        pid = node.get("parent_id")
        if pid:
            parent = conn.execute("SELECT * FROM nodes WHERE id = ?", (pid,)).fetchone()
            if parent:
                ancestors.append(dict(parent))
            current_id = pid
        else:
            break
    return ancestors


def get_branch_context(
    conn: sqlite3.Connection,
    scope_node_id: str,
    *,
    max_importance: int = 3,
) -> str:
    """Build branch-scoped context for a research run.

    Includes:
    - Root node (always)
    - Ancestors of the scope node
    - The scope node's subtree, filtered by importance level
    - Sibling nodes at ancestor level (headlines only)
    """
    parts: list[str] = []

    ancestors = get_ancestors(conn, scope_node_id)
    root = ancestors[-1] if ancestors else None
    if not root:
        row = conn.execute("SELECT * FROM nodes WHERE id = ?", (scope_node_id,)).fetchone()
        if row:
            root = dict(row)
    if not root:
        return "(empty context)"

    parts.append("# Root")
    parts.append(format_tree({"children": [], **root}, depth=0))
    parts.append("")

    if ancestors:
        parts.append("# Ancestor chain")
        for a in reversed(ancestors):
            nid = a["id"][:8]
            parts.append(f"  [{a['node_type']}] {a['headline']} [{nid}]")
        parts.append("")

        for a in ancestors[:-1]:
            siblings = conn.execute(
                "SELECT id, node_type, headline, importance FROM nodes "
                "WHERE parent_id = ? AND id != ? ORDER BY position",
                (a.get("parent_id", ""), a["id"]),
            ).fetchall()
            if siblings:
                parts.append(f"# Siblings of {a['headline'][:40]}")
                for s in siblings:
                    sd = dict(s)
                    parts.append(f"  [{sd['node_type']}] {sd['headline']} [{sd['id'][:8]}] L{sd['importance']}")
                parts.append("")

    parts.append("# Scoped branch (filtered to importance <= {})".format(max_importance))

    def render_filtered(node: dict, depth: int = 0) -> None:
        imp = node.get("importance", 0)
        if imp > max_importance and depth > 0:
            return
        indent = "  " * depth
        nid = node.get("id", "?")[:8]
        scores = ""
        if node.get("credence") is not None:
            scores += f" C{node['credence']}"
        if node.get("robustness") is not None:
            scores += f"/R{node['robustness']}"
        parts.append(f"{indent}[{node.get('node_type', '?')}] {node.get('headline', '?')} [{nid}] L{imp}{scores}")
        if node.get("content") and depth < 4:
            parts.append(f"{indent}  {node['content'][:300]}")
        for child in node.get("children", []):
            render_filtered(child, depth + 1)

    scope_tree = get_subtree(conn, scope_node_id)
    render_filtered(scope_tree)

    return "\n".join(parts)


def get_branch_health(conn: sqlite3.Connection, node_id: str) -> dict:
    """Quick diagnostic of a branch's state."""
    tree = get_subtree(conn, node_id)
    stats: dict[str, int] = {"total": 0, "claims": 0, "hypotheses": 0,
                              "evidence": 0, "uncertainties": 0, "questions": 0,
                              "max_depth": 0, "leafs_without_content": 0,
                              "no_credence": 0}

    def walk(node: dict, depth: int = 0) -> None:
        stats["total"] += 1
        stats["max_depth"] = max(stats["max_depth"], depth)
        nt = node.get("node_type", "")
        if nt == "claim":
            stats["claims"] += 1
        elif nt == "hypothesis":
            stats["hypotheses"] += 1
        elif nt == "evidence":
            stats["evidence"] += 1
        elif nt == "uncertainty":
            stats["uncertainties"] += 1
        elif nt == "question":
            stats["questions"] += 1
        if nt in ("claim", "hypothesis") and not node.get("credence"):
            stats["no_credence"] += 1
        children = node.get("children", [])
        if not children and not node.get("content"):
            stats["leafs_without_content"] += 1
        for c in children:
            walk(c, depth + 1)

    walk(tree)
    return stats


ORCHESTRATOR_TOOLS: list[dict[str, Any]] = [
    {
        "name": "add_node",
        "description": (
            "Add a new node to the current branch. This is a direct write — "
            "the node will be immediately visible in the worldview."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "node_type": {
                    "type": "string",
                    "enum": ["claim", "hypothesis", "evidence", "uncertainty", "context", "question"],
                },
                "headline": {"type": "string"},
                "content": {"type": "string", "description": "Detailed explanation"},
                "parent_id": {"type": "string", "description": "Short ID of the parent node in this branch"},
                "importance": {"type": "integer", "description": "0 = most important (L0), higher = less important"},
                "credence": {"type": "integer", "description": "1-9 confidence (for claims/hypotheses)"},
                "robustness": {"type": "integer", "description": "1-5 evidence quality"},
            },
            "required": ["node_type", "headline", "content", "parent_id"],
        },
    },
    {
        "name": "suggest_change",
        "description": (
            "Suggest a change that affects another branch or re-levels a node. "
            "This is NOT applied immediately — it goes into a review queue."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "suggestion_type": {
                    "type": "string",
                    "enum": ["add_to_branch", "relevel_node", "resolve_tension", "merge_duplicate"],
                    "description": "What kind of change",
                },
                "target_node_id": {"type": "string", "description": "Short ID of the node this affects"},
                "reasoning": {"type": "string", "description": "Why this change should be made"},
                "payload": {
                    "type": "object",
                    "description": "Details of the change (varies by type)",
                },
            },
            "required": ["suggestion_type", "target_node_id", "reasoning"],
        },
    },
    {
        "name": "relevel_node",
        "description": (
            "Change the importance level of a node in the current branch. "
            "Use when evidence has strengthened (move toward L0) or weakened (move toward L4+)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "node_id": {"type": "string", "description": "Short ID"},
                "new_importance": {"type": "integer", "description": "New L-level (0 = most important)"},
                "reasoning": {"type": "string"},
            },
            "required": ["node_id", "new_importance", "reasoning"],
        },
    },
    {
        "name": "inspect_branch",
        "description": "View the current branch's full context and health diagnostics.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
]


def execute_orchestrator_tool(
    conn: sqlite3.Connection,
    ws_id: str,
    scope_node_id: str,
    name: str,
    tool_input: dict,
    run_id: str,
    *,
    dry_run: bool = False,
) -> str:
    """Execute an orchestrator tool. When dry_run=True, describe what would happen but don't mutate."""
    action_id = new_id()
    prefix = "[dry-run] " if dry_run else ""

    if name == "add_node":
        parent_short = tool_input.get("parent_id", "")
        parent_full = resolve_node_id(conn, parent_short)
        if not parent_full:
            output = f"Parent node '{parent_short}' not found"
        elif dry_run:
            output = (
                f"{prefix}Would create [{tool_input.get('node_type', 'claim')}] "
                f"under {parent_short}: {tool_input['headline']}"
            )
        else:
            node_id = new_id()
            conn.execute(
                "INSERT INTO nodes (id, workspace_id, parent_id, node_type, headline, content, "
                "credence, robustness, importance, position, created_at, created_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    node_id, ws_id, parent_full,
                    tool_input.get("node_type", "claim"),
                    tool_input["headline"],
                    tool_input.get("content", ""),
                    tool_input.get("credence"),
                    tool_input.get("robustness"),
                    tool_input.get("importance", 2),
                    0, now_iso(), "orchestrator",
                ),
            )
            conn.commit()
            output = f"Created [{tool_input.get('node_type', 'claim')}] node {node_id[:8]}: {tool_input['headline']}"

    elif name == "suggest_change":
        if dry_run:
            output = (
                f"{prefix}Would queue suggestion: {tool_input.get('suggestion_type', '?')} "
                f"on {tool_input.get('target_node_id', '?')}: {tool_input.get('reasoning', '')[:100]}"
            )
        else:
            sug_id = new_id()
            conn.execute(
                "INSERT INTO suggestions (id, workspace_id, run_id, suggestion_type, "
                "target_node_id, payload, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    sug_id, ws_id, run_id,
                    tool_input.get("suggestion_type", ""),
                    resolve_node_id(conn, tool_input.get("target_node_id", "")) or "",
                    json.dumps({"reasoning": tool_input.get("reasoning", ""), **tool_input.get("payload", {})}),
                    now_iso(),
                ),
            )
            conn.commit()
            output = f"Queued suggestion {sug_id[:8]}: {tool_input.get('suggestion_type', '?')} on {tool_input.get('target_node_id', '?')}"

    elif name == "relevel_node":
        node_short = tool_input.get("node_id", "")
        full_id = resolve_node_id(conn, node_short)
        new_imp = tool_input.get("new_importance", 0)
        if not full_id:
            output = f"Node '{node_short}' not found"
        elif dry_run:
            output = f"{prefix}Would relevel {node_short} to L{new_imp}: {tool_input.get('reasoning', '')[:100]}"
        else:
            conn.execute("UPDATE nodes SET importance = ? WHERE id = ?", (new_imp, full_id))
            conn.commit()
            output = f"Releveled {node_short} to L{new_imp}"

    elif name == "inspect_branch":
        context = get_branch_context(conn, scope_node_id)
        health = get_branch_health(conn, scope_node_id)
        output = f"{context}\n\n# Branch health\n{json.dumps(health, indent=2)}"

    else:
        output = f"Unknown tool: {name}"

    conn.execute(
        "INSERT INTO actions (id, run_id, action_type, input_data, output_data, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (action_id, run_id, f"orch:{name}", json.dumps(tool_input), output[:2000], now_iso()),
    )
    conn.commit()
    return output


async def run_orchestrator_step(
    conn: sqlite3.Connection,
    ws_id: str,
    scope_node_id: str,
    run_id: str,
    api_key: str,
    *,
    dry_run: bool = True,
) -> dict:
    """Run one orchestrator step: assess a branch and produce improvements.

    Returns {"actions_taken": [...], "response": "..."}.
    """
    context = get_branch_context(conn, scope_node_id)
    health = get_branch_health(conn, scope_node_id)

    scope_node = conn.execute("SELECT * FROM nodes WHERE id = ?", (scope_node_id,)).fetchone()
    scope_headline = dict(scope_node)["headline"] if scope_node else "?"

    pending = conn.execute(
        "SELECT * FROM suggestions WHERE workspace_id = ? AND status = 'pending' "
        "ORDER BY created_at LIMIT 10",
        (ws_id,),
    ).fetchall()
    pending_text = ""
    if pending:
        pending_text = "\n\n# Pending suggestions\n"
        for s in pending:
            sd = dict(s)
            pending_text += f"  [{sd['id'][:8]}] {sd['suggestion_type']}: {sd.get('payload', '')[:100]}\n"

    system_prompt = (
        "You are a research orchestrator improving a worldview tree branch.\n\n"
        "Your job is to strengthen this branch by:\n"
        "1. Adding missing evidence, claims, or questions where gaps exist\n"
        "2. Re-leveling nodes whose importance has shifted (L0 = most important)\n"
        "3. Suggesting changes for other branches when you notice tensions or connections\n"
        "4. Flagging contradictions or unsupported claims\n\n"
        "## Rules\n"
        "- Add nodes to your own branch directly (add_node)\n"
        "- For changes to other branches, use suggest_change\n"
        "- Re-level nodes when evidence warrants it (relevel_node)\n"
        "- Use inspect_branch to see context and health before acting\n"
        "- Be conservative: a few high-quality additions beat many shallow ones\n"
        "- Assign importance levels honestly: L0 for crucial findings, L3+ for supplementary\n"
        "- Every claim should have credence and robustness scores\n\n"
        f"{'DRY RUN: describe what you would do but tools will not actually mutate.' if dry_run else ''}\n"
    )

    user_message = (
        f"# Branch: {scope_headline}\n\n"
        f"## Current state\n{context}\n\n"
        f"## Branch health\n{json.dumps(health, indent=2)}"
        f"{pending_text}\n\n"
        "Assess this branch and make improvements. Start by inspecting, then act."
    )

    client = anthropic.AsyncAnthropic(api_key=api_key)
    messages: list[dict] = [{"role": "user", "content": user_message}]
    actions_log: list[dict] = []

    for _ in range(8):
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            temperature=0.5,
            system=system_prompt,
            messages=messages,  # type: ignore[arg-type]
            tools=ORCHESTRATOR_TOOLS,  # type: ignore[arg-type]
        )

        text_parts: list[str] = []
        tool_calls: list[ToolUseBlock] = []
        for block in response.content:
            if isinstance(block, TextBlock):
                text_parts.append(block.text)
            elif isinstance(block, ToolUseBlock):
                tool_calls.append(block)

        if not tool_calls:
            return {"actions_taken": actions_log, "response": "\n".join(text_parts)}

        messages.append({"role": "assistant", "content": response.content})  # type: ignore[arg-type]

        tool_results = []
        for tc in tool_calls:
            result = execute_orchestrator_tool(
                conn, ws_id, scope_node_id, tc.name, tc.input, run_id,  # type: ignore[arg-type]
                dry_run=dry_run,
            )
            actions_log.append({"tool": tc.name, "input": tc.input, "result": result[:300]})  # type: ignore[arg-type]
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tc.id,
                "content": result,
            })

        messages.append({"role": "user", "content": tool_results})

    return {"actions_taken": actions_log, "response": "Reached max orchestrator rounds."}


def pick_next_branch(conn: sqlite3.Connection, ws_id: str) -> str | None:
    """Simple prioritizer: pick the L0 branch with the worst health score."""
    root = conn.execute(
        "SELECT id FROM nodes WHERE workspace_id = ? AND parent_id IS NULL LIMIT 1",
        (ws_id,),
    ).fetchone()
    if not root:
        return None

    children = conn.execute(
        "SELECT id, headline FROM nodes WHERE parent_id = ? ORDER BY position",
        (dict(root)["id"],),
    ).fetchall()

    best_id = None
    worst_score = float("inf")
    for child in children:
        child_dict = dict(child)
        health = get_branch_health(conn, child_dict["id"])
        score = health["total"] + health["evidence"] * 2 - health["no_credence"] * 3 - health["leafs_without_content"] * 2
        if score < worst_score:
            worst_score = score
            best_id = child_dict["id"]

    return best_id


TOOLS: list[dict[str, Any]] = [
    {
        "name": "search_workspace",
        "description": (
            "Search the research workspace by keyword. "
            "Returns the most relevant nodes (claims, hypotheses, evidence, etc.)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search terms"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_node",
        "description": "Fetch a node by its short ID. Returns full content and children.",
        "input_schema": {
            "type": "object",
            "properties": {
                "node_id": {"type": "string", "description": "8-character node ID"},
            },
            "required": ["node_id"],
        },
    },
    {
        "name": "create_node",
        "description": (
            "Add a new node to the worldview tree. "
            "Specify parent_id to place it under an existing node."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "node_type": {
                    "type": "string",
                    "enum": ["claim", "hypothesis", "evidence", "uncertainty", "context", "question"],
                },
                "headline": {"type": "string"},
                "content": {"type": "string"},
                "parent_id": {"type": "string", "description": "Short ID of the parent node"},
                "credence": {"type": "integer", "description": "1-9 confidence score"},
                "robustness": {"type": "integer", "description": "1-5 evidence quality score"},
            },
            "required": ["node_type", "headline"],
        },
    },
    {
        "name": "list_workspace",
        "description": "Show the full worldview tree structure at a glance.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "get_suggestions",
        "description": (
            "View the review queue — pending suggestions from the orchestrator "
            "for cross-branch changes, re-leveling, tension resolution, etc."
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
]


def resolve_node_id(conn: sqlite3.Connection, short_id: str) -> str | None:
    row = conn.execute("SELECT id FROM nodes WHERE id LIKE ?", (f"{short_id}%",)).fetchone()
    return dict(row)["id"] if row else None


def execute_tool(
    conn: sqlite3.Connection,
    ws_id: str,
    root_id: str,
    name: str,
    tool_input: dict,
    run_id: str,
) -> str:
    action_id = new_id()

    if name == "search_workspace":
        query = tool_input["query"]
        results = search_nodes(conn, ws_id, query)
        if not results:
            output = "No matching nodes found."
        else:
            lines = [f"Found {len(results)} nodes:\n"]
            for r in results:
                nid = r["id"][:8]
                scores = ""
                if r.get("credence"):
                    scores = f" C{r['credence']}"
                if r.get("robustness"):
                    scores += f"/R{r['robustness']}"
                lines.append(f"  [{r['node_type']}] {r['headline']} [{nid}]{scores}")
                if r.get("content"):
                    lines.append(f"    {r['content'][:200]}")
                lines.append("")
            output = "\n".join(lines)

    elif name == "get_node":
        full_id = resolve_node_id(conn, tool_input["node_id"])
        if not full_id:
            output = f"No node matching '{tool_input['node_id']}'"
        else:
            tree = get_subtree(conn, full_id)
            output = format_tree(tree)

    elif name == "create_node":
        parent_short = tool_input.get("parent_id")
        parent_full = resolve_node_id(conn, parent_short) if parent_short else root_id
        if not parent_full:
            output = f"Parent node '{parent_short}' not found"
        else:
            node_id = new_id()
            conn.execute(
                "INSERT INTO nodes (id, workspace_id, parent_id, node_type, headline, content, "
                "credence, robustness, position, created_at, created_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    node_id, ws_id, parent_full,
                    tool_input.get("node_type", "claim"),
                    tool_input["headline"],
                    tool_input.get("content", ""),
                    tool_input.get("credence"),
                    tool_input.get("robustness"),
                    0, now_iso(), "agent",
                ),
            )
            conn.commit()
            output = f"Created [{tool_input.get('node_type', 'claim')}] node {node_id[:8]}: {tool_input['headline']}"

    elif name == "list_workspace":
        tree = get_subtree(conn, root_id)
        output = format_tree(tree)

    elif name == "get_suggestions":
        status = tool_input.get("status", "pending")
        rows = conn.execute(
            "SELECT s.*, n.headline as target_headline FROM suggestions s "
            "LEFT JOIN nodes n ON s.target_node_id = n.id "
            "WHERE s.workspace_id = ? AND s.status = ? ORDER BY s.created_at DESC LIMIT 20",
            (ws_id, status),
        ).fetchall()
        if not rows:
            output = f"No {status} suggestions."
        else:
            lines = [f"{len(rows)} {status} suggestion(s):\n"]
            for r in rows:
                rd = dict(r)
                target = rd.get("target_headline", rd.get("target_node_id", "?")[:8])
                payload = json.loads(rd.get("payload", "{}"))
                reasoning = payload.get("reasoning", "")[:150]
                lines.append(f"  [{rd['id'][:8]}] {rd['suggestion_type']} → {target}")
                if reasoning:
                    lines.append(f"    {reasoning}")
                lines.append("")
            output = "\n".join(lines)

    else:
        output = f"Unknown tool: {name}"

    conn.execute(
        "INSERT INTO actions (id, run_id, action_type, input_data, output_data, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (action_id, run_id, name, json.dumps(tool_input), output[:2000], now_iso()),
    )
    conn.commit()
    return output


MODEL_MAP = {
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-6",
    "haiku": "claude-haiku-4-5-20251001",
}


class ChatRequest(BaseModel):
    question_id: str | None = None
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    seed_from_python_mock()
    yield


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/api/workspaces")
def list_workspaces():
    conn = get_db()
    rows = conn.execute("SELECT * FROM workspaces ORDER BY created_at").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/workspaces/{name}/tree")
def get_workspace_tree(name: str):
    conn = get_db()
    ws = conn.execute("SELECT * FROM workspaces WHERE name = ?", (name,)).fetchone()
    if not ws:
        conn.close()
        return {"error": "workspace not found"}
    ws_id = dict(ws)["id"]
    root = conn.execute(
        "SELECT * FROM nodes WHERE workspace_id = ? AND parent_id IS NULL ORDER BY position LIMIT 1",
        (ws_id,),
    ).fetchone()
    if not root:
        conn.close()
        return {"error": "no root node"}
    tree = get_subtree(conn, dict(root)["id"])
    conn.close()
    return tree


@app.get("/api/runs")
def list_runs(workspace: str = "default", limit: int = 20):
    conn = get_db()
    ws = conn.execute("SELECT id FROM workspaces WHERE name = ?", (workspace,)).fetchone()
    if not ws:
        conn.close()
        return []
    ws_id = dict(ws)["id"]
    rows = conn.execute(
        "SELECT r.*, "
        "(SELECT COUNT(*) FROM actions a WHERE a.run_id = r.id) as action_count "
        "FROM runs r WHERE r.workspace_id = ? ORDER BY r.started_at DESC LIMIT ?",
        (ws_id, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/runs/{run_id}/actions")
def get_run_actions(run_id: str):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM actions WHERE run_id = ? ORDER BY created_at",
        (run_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/workspaces/{name}/suggestions")
def list_suggestions(name: str, status: str = "pending"):
    conn = get_db()
    ws = conn.execute("SELECT id FROM workspaces WHERE name = ?", (name,)).fetchone()
    if not ws:
        conn.close()
        return []
    ws_id = dict(ws)["id"]
    rows = conn.execute(
        "SELECT s.*, n.headline as target_headline FROM suggestions s "
        "LEFT JOIN nodes n ON s.target_node_id = n.id "
        "WHERE s.workspace_id = ? AND s.status = ? ORDER BY s.created_at DESC",
        (ws_id, status),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/suggestions/{suggestion_id}/accept")
def accept_suggestion(suggestion_id: str):
    conn = get_db()
    sug = conn.execute("SELECT * FROM suggestions WHERE id = ?", (suggestion_id,)).fetchone()
    if not sug:
        conn.close()
        return {"error": "suggestion not found"}
    sd = dict(sug)
    payload = json.loads(sd.get("payload", "{}"))

    if sd["suggestion_type"] == "relevel_node" and sd["target_node_id"]:
        new_imp = payload.get("new_importance", 0)
        conn.execute("UPDATE nodes SET importance = ? WHERE id = ?", (new_imp, sd["target_node_id"]))

    conn.execute(
        "UPDATE suggestions SET status = 'accepted', reviewed_at = ? WHERE id = ?",
        (now_iso(), suggestion_id),
    )
    conn.commit()
    conn.close()
    return {"status": "accepted"}


@app.post("/api/suggestions/{suggestion_id}/reject")
def reject_suggestion(suggestion_id: str):
    conn = get_db()
    conn.execute(
        "UPDATE suggestions SET status = 'rejected', reviewed_at = ? WHERE id = ?",
        (now_iso(), suggestion_id),
    )
    conn.commit()
    conn.close()
    return {"status": "rejected"}


@app.get("/api/workspaces/{name}/branch-context/{node_id}")
def get_branch_context_endpoint(name: str, node_id: str, max_importance: int = 3):
    conn = get_db()
    ws = conn.execute("SELECT id FROM workspaces WHERE name = ?", (name,)).fetchone()
    if not ws:
        conn.close()
        return {"error": "workspace not found"}
    full_id = resolve_node_id(conn, node_id)
    if not full_id:
        conn.close()
        return {"error": f"node {node_id} not found"}
    context = get_branch_context(conn, full_id, max_importance=max_importance)
    health = get_branch_health(conn, full_id)
    conn.close()
    return {"context": context, "health": health}


@app.post("/api/workspaces/{name}/orchestrate")
async def orchestrate(name: str, node_id: str | None = None, dry_run: bool = True):
    """Run one orchestrator step on a branch.

    If node_id is omitted, the prioritizer picks the weakest branch.
    dry_run=True (default) means tools describe actions but don't mutate.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        secrets_path = Path(__file__).parent.parent / "secrets.env"
        if secrets_path.exists():
            for line in secrets_path.read_text().splitlines():
                cleaned = line.removeprefix("export ").strip()
                if cleaned.startswith("ANTHROPIC_API_KEY="):
                    api_key = cleaned.split("=", 1)[1].strip()
                    break
    if not api_key:
        return {"error": "ANTHROPIC_API_KEY not set"}

    conn = get_db()
    ws = conn.execute("SELECT * FROM workspaces WHERE name = ?", (name,)).fetchone()
    if not ws:
        conn.close()
        return {"error": "workspace not found"}
    ws_id = dict(ws)["id"]

    if node_id:
        scope_id = resolve_node_id(conn, node_id)
    else:
        scope_id = pick_next_branch(conn, ws_id)

    if not scope_id:
        conn.close()
        return {"error": "no branch to orchestrate"}

    scope_node = conn.execute("SELECT headline FROM nodes WHERE id = ?", (scope_id,)).fetchone()
    scope_headline = dict(scope_node)["headline"] if scope_node else "?"

    run_id = new_id()
    conn.execute(
        "INSERT INTO runs (id, workspace_id, run_type, scope_node_id, started_at, status, description, config) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, ws_id, "orchestrate", scope_id, now_iso(), "running",
         f"orchestrate: {scope_headline[:60]}", json.dumps({"dry_run": dry_run})),
    )
    conn.commit()

    result = await run_orchestrator_step(conn, ws_id, scope_id, run_id, api_key, dry_run=dry_run)

    conn.execute(
        "UPDATE runs SET status = 'completed', completed_at = ? WHERE id = ?",
        (now_iso(), run_id),
    )
    conn.commit()
    conn.close()

    return {
        "run_id": run_id,
        "scope_node": scope_headline,
        "scope_node_id": scope_id[:8],
        "dry_run": dry_run,
        **result,
    }


@app.post("/api/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    # Check environment, then secrets.env
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        secrets_path = Path(__file__).parent.parent / "secrets.env"
        if secrets_path.exists():
            for line in secrets_path.read_text().splitlines():
                cleaned = line.removeprefix("export ").strip()
                if cleaned.startswith("ANTHROPIC_API_KEY="):
                    api_key = cleaned.split("=", 1)[1].strip()
                    break
    if not api_key:
        return ChatResponse(
            response="ANTHROPIC_API_KEY not set. Export it and restart the server.",
            tool_uses=[],
        )

    conn = get_db()
    ws = conn.execute(
        "SELECT * FROM workspaces WHERE name = ?", (request.workspace,)
    ).fetchone()
    if not ws:
        conn.close()
        return ChatResponse(response=f"Workspace '{request.workspace}' not found.", tool_uses=[])

    ws_dict = dict(ws)
    ws_id = ws_dict["id"]

    root = conn.execute(
        "SELECT * FROM nodes WHERE workspace_id = ? AND parent_id IS NULL ORDER BY position LIMIT 1",
        (ws_id,),
    ).fetchone()
    if not root:
        conn.close()
        return ChatResponse(response="No root node in workspace.", tool_uses=[])

    root_dict = dict(root)
    root_id = root_dict["id"]

    run_id = new_id()
    conn.execute(
        "INSERT INTO runs (id, workspace_id, started_at, status, description) VALUES (?, ?, ?, ?, ?)",
        (run_id, ws_id, now_iso(), "running", "chat"),
    )
    conn.commit()

    tree = get_subtree(conn, root_id)
    context = format_tree(tree)

    prompt_path = PROMPTS_DIR / "api_chat.md"
    system_prompt = prompt_path.read_text() if prompt_path.exists() else "You are a research assistant."
    full_system = f"{system_prompt}\n\n---\n\n# Current worldview\n\n{context}"

    model_id = MODEL_MAP.get(request.model, MODEL_MAP["sonnet"])
    client = anthropic.AsyncAnthropic(api_key=api_key)
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
            conn.execute(
                "UPDATE runs SET status = 'completed', completed_at = ? WHERE id = ?",
                (now_iso(), run_id),
            )
            conn.commit()
            conn.close()
            return ChatResponse(response="\n".join(text_parts), tool_uses=tool_uses_log)

        messages.append({"role": "assistant", "content": response.content})  # type: ignore[arg-type]

        tool_results = []
        for tc in tool_calls:
            result_str = execute_tool(conn, ws_id, root_id, tc.name, tc.input, run_id)  # type: ignore[arg-type]
            tool_uses_log.append(ToolUseInfo(name=tc.name, input=tc.input, result=result_str[:500]))  # type: ignore[arg-type]
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tc.id,
                "content": result_str,
            })

        messages.append({"role": "user", "content": tool_results})

    conn.execute(
        "UPDATE runs SET status = 'completed', completed_at = ? WHERE id = ?",
        (now_iso(), run_id),
    )
    conn.commit()
    conn.close()
    return ChatResponse(response="Reached maximum tool rounds.", tool_uses=tool_uses_log)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8099)
