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
from collections.abc import Callable
from typing import Any

import anthropic
import uvicorn
from anthropic.types import TextBlock, ToolUseBlock
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

# Add orchestrator package to import path
import sys as _sys
_sys.path.insert(0, str(Path(__file__).parent))

from orchestrator import (
    build_branch_context,
    format_tree,
    get_branch_health,
    get_subtree,
    list_run_types,
    make_tool_executor,
    pick_next_branch,
    preview_branch_context,
    resolve_run_type,
    run_step,
)
from orchestrator.tracing import RunTracer

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
            node_type TEXT NOT NULL CHECK(node_type IN ('claim','hypothesis','evidence','uncertainty','context','question','judgement','concept')),
            headline TEXT NOT NULL,
            content TEXT NOT NULL DEFAULT '',
            credence INTEGER CHECK(credence BETWEEN 1 AND 9),
            robustness INTEGER CHECK(robustness BETWEEN 1 AND 5),
            importance INTEGER NOT NULL DEFAULT 0,
            position INTEGER NOT NULL DEFAULT 0,
            source_ids TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            created_by TEXT NOT NULL DEFAULT 'system',
            superseded_by TEXT REFERENCES nodes(id)
        );

        CREATE TABLE IF NOT EXISTS node_links (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(id),
            source_id TEXT NOT NULL REFERENCES nodes(id),
            target_id TEXT NOT NULL REFERENCES nodes(id),
            link_type TEXT NOT NULL CHECK(link_type IN ('supports','opposes','depends_on','related')),
            strength INTEGER CHECK(strength BETWEEN 1 AND 5),
            reasoning TEXT NOT NULL DEFAULT '',
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

        CREATE TABLE IF NOT EXISTS sources (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(id),
            title TEXT NOT NULL,
            url TEXT,
            abstract TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL DEFAULT '',
            extra TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS trace_events (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES runs(id),
            event_type TEXT NOT NULL,
            span_id TEXT NOT NULL,
            parent_span_id TEXT,
            timestamp TEXT NOT NULL,
            data TEXT NOT NULL DEFAULT '{}'
        );

        CREATE INDEX IF NOT EXISTS idx_nodes_workspace ON nodes(workspace_id);
        CREATE INDEX IF NOT EXISTS idx_nodes_parent ON nodes(parent_id);
        CREATE INDEX IF NOT EXISTS idx_nodes_importance ON nodes(workspace_id, importance);
        CREATE INDEX IF NOT EXISTS idx_runs_workspace ON runs(workspace_id);
        CREATE INDEX IF NOT EXISTS idx_actions_run ON actions(run_id);
        CREATE INDEX IF NOT EXISTS idx_suggestions_workspace ON suggestions(workspace_id, status);
        CREATE INDEX IF NOT EXISTS idx_sources_workspace ON sources(workspace_id);
        CREATE INDEX IF NOT EXISTS idx_trace_events_run ON trace_events(run_id);
        CREATE INDEX IF NOT EXISTS idx_trace_events_span ON trace_events(span_id);
        CREATE INDEX IF NOT EXISTS idx_node_links_source ON node_links(source_id);
        CREATE INDEX IF NOT EXISTS idx_node_links_target ON node_links(target_id);
        CREATE INDEX IF NOT EXISTS idx_node_links_workspace ON node_links(workspace_id);
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


def search_nodes(conn: sqlite3.Connection, ws_id: str, query: str, limit: int = 8) -> list[dict]:
    """Simple text search over nodes."""
    rows = conn.execute(
        "SELECT * FROM nodes WHERE workspace_id = ? "
        "AND (headline LIKE ? OR content LIKE ?) "
        "ORDER BY position LIMIT ?",
        (ws_id, f"%{query}%", f"%{query}%", limit),
    ).fetchall()
    return [dict(r) for r in rows]


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
    {
        "name": "preview_run",
        "description": (
            "Show the user a visual preview of an orchestrator run. "
            "This renders as an interactive component in chat — the user sees "
            "the branch tree, which nodes are in context, health stats, and "
            "action buttons to launch the run. Use this whenever the user asks "
            "to preview, plan, or prepare a run. Always call this INSTEAD OF "
            "run_orchestrator when the user wants to see what a run would do."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "node_id": {
                    "type": "string",
                    "description": "Short ID of branch to preview. Omit to auto-pick the weakest branch.",
                },
                "run_type": {
                    "type": "string",
                    "enum": ["explore", "evaluate"],
                    "description": "Which run type to preview. Default: explore.",
                },
            },
        },
    },
    {
        "name": "run_orchestrator",
        "description": (
            "EXECUTE an orchestrator run — this calls the LLM, which uses tools "
            "to add nodes, relevel, and suggest changes. Costs money and modifies "
            "the tree. Call ONLY after preview_run and explicit user confirmation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "node_id": {
                    "type": "string",
                    "description": "Short ID of the branch to orchestrate. Omit to auto-pick the weakest branch.",
                },
                "run_type": {
                    "type": "string",
                    "enum": ["explore", "evaluate"],
                    "description": "Run type. Default: explore.",
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

    elif name == "preview_run":
        target_short = tool_input.get("node_id")
        run_type = tool_input.get("run_type", "explore")
        target_full = resolve_node_id(conn, target_short) if target_short else pick_next_branch(conn, ws_id)
        if not target_full:
            output = json.dumps({"error": "No branch to preview."})
        else:
            try:
                config = resolve_run_type(run_type)
            except ValueError as e:
                output = json.dumps({"error": str(e)})
            else:
                preview = preview_branch_context(conn, target_full, ws_id=ws_id)
                preview["run_type"] = run_type
                preview["config"] = {
                    "max_rounds": config.get("max_rounds", 8),
                    "temperature": config.get("temperature", 0.5),
                    "dry_run": True,
                }
                preview["tools_available"] = [t["name"] for t in config["tools"]]
                output = json.dumps(preview)

    elif name == "run_orchestrator":
        # Async — handled by the chat endpoint. Return a sentinel with resolved params.
        target_short = tool_input.get("node_id")
        run_type = tool_input.get("run_type", "explore")
        dry = tool_input.get("dry_run", False)
        target_full = resolve_node_id(conn, target_short) if target_short else pick_next_branch(conn, ws_id)
        if not target_full:
            output = "No branch to orchestrate."
        else:
            # Store resolved params for the async handler to pick up
            output = json.dumps({
                "__async_orchestrate__": True,
                "scope_id": target_full,
                "run_type": run_type,
                "dry_run": dry,
                "ws_id": ws_id,
            })

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
    rows = conn.execute(
        "SELECT w.*, "
        "(SELECT COUNT(*) FROM nodes n WHERE n.workspace_id = w.id) as node_count, "
        "(SELECT COUNT(*) FROM sources src WHERE src.workspace_id = w.id) as source_count, "
        "(SELECT COUNT(*) FROM runs r WHERE r.workspace_id = w.id) as run_count, "
        "(SELECT COUNT(*) FROM suggestions s WHERE s.workspace_id = w.id AND s.status = 'pending') as pending_suggestions "
        "FROM workspaces w ORDER BY w.created_at"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/workspaces")
def create_workspace(name: str, question: str = ""):
    conn = get_db()
    existing = conn.execute("SELECT id FROM workspaces WHERE name = ?", (name,)).fetchone()
    if existing:
        conn.close()
        return {"error": f"Workspace '{name}' already exists", "id": dict(existing)["id"]}
    ws_id = new_id()
    conn.execute(
        "INSERT INTO workspaces (id, name, created_at) VALUES (?, ?, ?)",
        (ws_id, name, now_iso()),
    )
    root_id = None
    if question:
        root_id = new_id()
        conn.execute(
            "INSERT INTO nodes (id, workspace_id, parent_id, node_type, headline, content, "
            "importance, position, created_at) VALUES (?, ?, NULL, 'question', ?, '', 0, 0, ?)",
            (root_id, ws_id, question, now_iso()),
        )
    conn.commit()
    conn.close()
    return {"id": ws_id, "name": name, "root_node_id": root_id}


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


@app.get("/api/workspaces/{name}/sources")
def list_sources(name: str):
    conn = get_db()
    ws = conn.execute("SELECT id FROM workspaces WHERE name = ?", (name,)).fetchone()
    if not ws:
        conn.close()
        return []
    ws_id = dict(ws)["id"]
    rows = conn.execute(
        "SELECT id, workspace_id, title, url, abstract, created_at "
        "FROM sources WHERE workspace_id = ? ORDER BY created_at DESC",
        (ws_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/sources/short/{short_id}")
def get_source_by_short_id(short_id: str):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM sources WHERE id LIKE ?", (f"{short_id}%",)
    ).fetchone()
    conn.close()
    if not row:
        return {"error": f"No source matching '{short_id}'"}
    return dict(row)


@app.get("/api/sources/{source_id}")
def get_source(source_id: str):
    conn = get_db()
    row = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
    conn.close()
    if not row:
        return {"error": f"Source '{source_id}' not found"}
    return dict(row)


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
    context = build_branch_context(conn, full_id, max_importance=max_importance)
    health = get_branch_health(conn, full_id)
    conn.close()
    return {"context": context, "health": health}


@app.get("/api/workspaces/{name}/orchestrate-preview")
def orchestrate_preview(
    name: str,
    node_id: str | None = None,
    run_type: str = "explore",
    max_importance: int = 2,
):
    """Preview what an orchestrator run would see — structured data for frontend rendering."""
    try:
        config = resolve_run_type(run_type)
    except ValueError as e:
        return {"error": str(e)}

    conn = get_db()
    ws = conn.execute("SELECT id FROM workspaces WHERE name = ?", (name,)).fetchone()
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
        return {"error": "no branch to preview"}

    preview = preview_branch_context(conn, scope_id, max_importance=max_importance, ws_id=ws_id)
    conn.close()

    preview["run_type"] = run_type
    preview["config"] = {
        "max_rounds": config.get("max_rounds", 8),
        "temperature": config.get("temperature", 0.5),
        "dry_run": True,
    }
    preview["tools_available"] = [t["name"] for t in config["tools"]]
    return preview


@app.post("/api/workspaces/{name}/orchestrate")
async def orchestrate(
    name: str,
    node_id: str | None = None,
    dry_run: bool = True,
    run_type: str = "explore",
):
    """Run one orchestrator step on a branch.

    If node_id is omitted, the prioritizer picks the weakest branch.
    dry_run=True (default) means tools describe actions but don't mutate.
    run_type controls which prompt/tools/context layers are used (explore, evaluate).
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

    try:
        config = resolve_run_type(run_type)
    except ValueError as e:
        return {"error": str(e)}

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
         f"orchestrate ({run_type}): {scope_headline[:50]}",
         json.dumps({"dry_run": dry_run, "run_type": run_type})),
    )
    conn.commit()

    context = build_branch_context(
        conn, scope_id,
        layers=config["context_layers"],
        ws_id=ws_id,
    )
    system_prompt = config["system_prompt"]
    if dry_run:
        system_prompt += "\n\nDRY RUN: describe what you would do but tools will not actually mutate."

    user_message = (
        f"# Branch: {scope_headline}\n\n"
        f"{context}\n\n"
        "Assess this branch and make improvements. Start by inspecting, then act."
    )

    executor = make_tool_executor(
        conn, ws_id, scope_id, run_id,
        dry_run=dry_run,
        resolve_node_id=resolve_node_id,
        new_id=new_id,
        now_iso=now_iso,
    )

    tracer = RunTracer(conn=conn, run_id=run_id)
    result = await run_step(
        system_prompt=system_prompt,
        user_message=user_message,
        tools=config["tools"],
        execute_tool=executor,
        api_key=api_key,
        max_rounds=config.get("max_rounds", 8),
        max_tokens=config.get("max_tokens", 4096),
        temperature=config.get("temperature", 0.5),
        tracer=tracer,
    )

    tracer.finalize()
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
        "run_type": run_type,
        "actions_taken": result.actions_taken,
        "response": result.response,
        "rounds_used": result.rounds_used,
        "stop_reason": result.stop_reason,
    }


async def _run_orchestrator_from_chat(
    conn: sqlite3.Connection,
    params: dict,
    api_key: str | None,
    chat_run_id: str,
    on_progress: Callable[[str], None] | None = None,
) -> str:
    """Execute an orchestrator run triggered from chat. Returns a summary string.

    on_progress is called with a short status string after each tool action,
    so the streaming handler can emit progress events.
    """
    if not api_key:
        return "Cannot run orchestrator: ANTHROPIC_API_KEY not set."

    scope_id = params["scope_id"]
    run_type_name = params.get("run_type", "explore")
    dry_run = params.get("dry_run", False)
    ws_id = params["ws_id"]

    try:
        config = resolve_run_type(run_type_name)
    except ValueError as e:
        return str(e)

    scope_node = conn.execute(
        "SELECT headline FROM nodes WHERE id = ?", (scope_id,)
    ).fetchone()
    scope_headline = dict(scope_node)["headline"] if scope_node else "?"

    orch_run_id = new_id()
    conn.execute(
        "INSERT INTO runs (id, workspace_id, run_type, scope_node_id, started_at, status, description, config) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (orch_run_id, ws_id, "orchestrate", scope_id, now_iso(), "running",
         f"orchestrate ({run_type_name}): {scope_headline[:50]}",
         json.dumps({"dry_run": dry_run, "run_type": run_type_name, "from_chat": chat_run_id})),
    )
    conn.commit()

    if on_progress:
        on_progress(f"starting {run_type_name} on '{scope_headline[:40]}'...")

    context = build_branch_context(
        conn, scope_id, layers=config["context_layers"], ws_id=ws_id,
    )
    system_prompt = config["system_prompt"]
    if dry_run:
        system_prompt += "\n\nDRY RUN: describe what you would do but tools will not actually mutate."

    user_message = (
        f"# Branch: {scope_headline}\n\n"
        f"{context}\n\n"
        "Assess this branch and make improvements. Start by inspecting, then act."
    )

    executor = make_tool_executor(
        conn, ws_id, scope_id, orch_run_id,
        dry_run=dry_run,
        resolve_node_id=resolve_node_id,
        new_id=new_id,
        now_iso=now_iso,
    )

    tracer = RunTracer(conn=conn, run_id=orch_run_id)
    action_count = [0]

    def on_action(action: dict) -> None:
        action_count[0] += 1
        tool_name = action.get("tool", "?")
        result_preview = action.get("result", "")[:60]
        if on_progress:
            on_progress(f"[{action_count[0]}] {tool_name}: {result_preview}")

    result = await run_step(
        system_prompt=system_prompt,
        user_message=user_message,
        tools=config["tools"],
        execute_tool=executor,
        api_key=api_key,
        max_rounds=config.get("max_rounds", 8),
        max_tokens=config.get("max_tokens", 4096),
        temperature=config.get("temperature", 0.5),
        on_action=on_action,
        tracer=tracer,
    )

    trace_stats = tracer.finalize()
    conn.execute(
        "UPDATE runs SET status = 'completed', completed_at = ? WHERE id = ?",
        (now_iso(), orch_run_id),
    )
    conn.commit()

    actions_summary = "\n".join(
        f"  {a['tool']}: {a['result'][:100]}" for a in result.actions_taken
    )
    return (
        f"Orchestrator {run_type_name} on '{scope_headline[:40]}' "
        f"({'dry run' if dry_run else 'live'}, {result.rounds_used} rounds):\n"
        f"{actions_summary}\n\n"
        f"{result.response[:500]}"
    )


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
            # Handle async orchestrator runs
            if tc.name == "run_orchestrator" and "__async_orchestrate__" in result_str:
                params = json.loads(result_str)
                result_str = await _run_orchestrator_from_chat(
                    conn, params, api_key, run_id,
                )
            tool_uses_log.append(ToolUseInfo(name=tc.name, input=tc.input, result=result_str))  # type: ignore[arg-type]
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


def _get_api_key() -> str | None:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        secrets_path = Path(__file__).parent.parent / "secrets.env"
        if secrets_path.exists():
            for line in secrets_path.read_text().splitlines():
                cleaned = line.removeprefix("export ").strip()
                if cleaned.startswith("ANTHROPIC_API_KEY="):
                    api_key = cleaned.split("=", 1)[1].strip()
                    break
    return api_key


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@app.post("/api/chat/stream")
async def chat_stream(request: ChatRequest):

    api_key = _get_api_key()
    if not api_key:
        async def error_gen():
            yield _sse("error", {"message": "ANTHROPIC_API_KEY not set."})
        return StreamingResponse(error_gen(), media_type="text/event-stream")

    conn = get_db()
    ws = conn.execute(
        "SELECT * FROM workspaces WHERE name = ?", (request.workspace,)
    ).fetchone()
    if not ws:
        conn.close()
        async def error_gen():
            yield _sse("error", {"message": f"Workspace '{request.workspace}' not found."})
        return StreamingResponse(error_gen(), media_type="text/event-stream")

    ws_dict = dict(ws)
    ws_id = ws_dict["id"]

    root = conn.execute(
        "SELECT * FROM nodes WHERE workspace_id = ? AND parent_id IS NULL ORDER BY position LIMIT 1",
        (ws_id,),
    ).fetchone()
    if not root:
        conn.close()
        async def error_gen():
            yield _sse("error", {"message": "No root node in workspace."})
        return StreamingResponse(error_gen(), media_type="text/event-stream")

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

    async def generate():
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
                            if event.delta.type == "text_delta":
                                yield _sse("text", {"content": event.delta.text})
                        elif event.type == "content_block_start":
                            cb = event.content_block
                            if isinstance(cb, ToolUseBlock):
                                yield _sse("tool_use_start", {"name": cb.name})

                response = await stream.get_final_message()

                tool_calls = [b for b in response.content if isinstance(b, ToolUseBlock)]
                if not tool_calls:
                    break

                messages.append({"role": "assistant", "content": response.content})

                tool_results = []
                for tc in tool_calls:
                    result_str = execute_tool(conn, ws_id, root_id, tc.name, tc.input, run_id)
                    if tc.name == "run_orchestrator" and "__async_orchestrate__" in result_str:
                        params = json.loads(result_str)
                        progress_msgs: list[str] = []
                        result_str = await _run_orchestrator_from_chat(
                            conn, params, api_key, run_id,
                            on_progress=lambda msg: progress_msgs.append(msg),
                        )
                        for pmsg in progress_msgs:
                            yield _sse("orchestrator_progress", {"message": pmsg})
                    yield _sse("tool_use_result", {
                        "name": tc.name,
                        "input": tc.input,
                        "result": result_str,
                    })
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tc.id,
                        "content": result_str,
                    })
                messages.append({"role": "user", "content": tool_results})

            yield _sse("done", {})
        except Exception as e:
            yield _sse("error", {"message": str(e)})
        finally:
            conn.execute(
                "UPDATE runs SET status = 'completed', completed_at = ? WHERE id = ?",
                (now_iso(), run_id),
            )
            conn.commit()
            conn.close()

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.get("/api/operator/runs")
def operator_list_runs(
    workspace: str | None = None,
    run_type: str | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
):
    """List runs with aggregated trace stats for the operator UI."""
    conn = get_db()
    conditions = []
    params: list[object] = []
    if workspace:
        conditions.append("w.name = ?")
        params.append(workspace)
    if run_type:
        conditions.append("r.run_type = ?")
        params.append(run_type)
    if status:
        conditions.append("r.status = ?")
        params.append(status)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    count_row = conn.execute(
        f"SELECT COUNT(*) FROM runs r JOIN workspaces w ON r.workspace_id = w.id {where}",
        params,
    ).fetchone()
    total = count_row[0] if count_row else 0

    rows = conn.execute(
        f"SELECT r.*, w.name as workspace_name, n.headline as scope_node_headline "
        f"FROM runs r "
        f"JOIN workspaces w ON r.workspace_id = w.id "
        f"LEFT JOIN nodes n ON r.scope_node_id = n.id "
        f"{where} "
        f"ORDER BY r.started_at DESC LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()

    runs = []
    for row in rows:
        rd = dict(row)
        run_id = rd["id"]
        trace_stats = conn.execute(
            "SELECT "
            "  COUNT(*) FILTER (WHERE event_type = 'model') as model_calls, "
            "  COUNT(*) FILTER (WHERE event_type = 'tool') as tool_calls, "
            "  COALESCE(SUM(CASE WHEN event_type = 'model' THEN json_extract(data, '$.cost_usd') END), 0) as total_cost, "
            "  COALESCE(SUM(CASE WHEN event_type = 'model' THEN json_extract(data, '$.usage.input_tokens') END), 0) as input_tokens, "
            "  COALESCE(SUM(CASE WHEN event_type = 'model' THEN json_extract(data, '$.usage.output_tokens') END), 0) as output_tokens, "
            "  COALESCE(SUM(CASE WHEN event_type = 'model' THEN json_extract(data, '$.usage.cache_read_tokens') END), 0) as cache_read, "
            "  COALESCE(SUM(CASE WHEN event_type = 'model' THEN json_extract(data, '$.usage.cache_write_tokens') END), 0) as cache_write "
            "FROM trace_events WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        ts = dict(trace_stats) if trace_stats else {}

        started = rd.get("started_at", "")
        completed = rd.get("completed_at")
        duration_ms = 0
        if started and completed:
            try:
                from datetime import datetime as dt
                t0 = dt.fromisoformat(started)
                t1 = dt.fromisoformat(completed)
                duration_ms = int((t1 - t0).total_seconds() * 1000)
            except (ValueError, TypeError):
                pass

        runs.append({
            "id": run_id,
            "workspace_id": rd["workspace_id"],
            "workspace_name": rd.get("workspace_name", ""),
            "run_type": rd["run_type"],
            "status": rd["status"],
            "started_at": started,
            "completed_at": completed,
            "description": rd.get("description"),
            "scope_node_headline": rd.get("scope_node_headline"),
            "total_cost_usd": ts.get("total_cost", 0),
            "total_usage": {
                "input_tokens": int(ts.get("input_tokens", 0)),
                "output_tokens": int(ts.get("output_tokens", 0)),
                "cache_read_tokens": int(ts.get("cache_read", 0)),
                "cache_write_tokens": int(ts.get("cache_write", 0)),
            },
            "model_call_count": int(ts.get("model_calls", 0)),
            "tool_call_count": int(ts.get("tool_calls", 0)),
            "duration_ms": duration_ms,
        })

    conn.close()
    return {"runs": runs, "total": total}


@app.get("/api/operator/runs/{run_id}")
def operator_run_detail(run_id: str):
    """Full run detail with all trace events for the operator UI."""
    conn = get_db()
    row = conn.execute(
        "SELECT r.*, w.name as workspace_name, n.headline as scope_node_headline "
        "FROM runs r "
        "JOIN workspaces w ON r.workspace_id = w.id "
        "LEFT JOIN nodes n ON r.scope_node_id = n.id "
        "WHERE r.id = ?",
        (run_id,),
    ).fetchone()
    if not row:
        conn.close()
        return {"error": "run not found"}
    rd = dict(row)

    event_rows = conn.execute(
        "SELECT * FROM trace_events WHERE run_id = ? ORDER BY timestamp",
        (run_id,),
    ).fetchall()

    events = []
    total_cost = 0.0
    total_input = 0
    total_output = 0
    total_cache_read = 0
    total_cache_write = 0
    model_calls = 0
    tool_calls = 0

    for er in event_rows:
        ed = dict(er)
        data = json.loads(ed.get("data", "{}"))
        event: dict[str, Any] = {
            "event_type": ed["event_type"],
            "id": ed["id"],
            "span_id": ed["span_id"],
            "timestamp": ed["timestamp"],
        }
        if ed.get("parent_span_id"):
            event["parent_span_id"] = ed["parent_span_id"]

        if ed["event_type"] == "model":
            event.update(data)
            total_cost += data.get("cost_usd", 0)
            usage = data.get("usage", {})
            total_input += usage.get("input_tokens", 0)
            total_output += usage.get("output_tokens", 0)
            total_cache_read += usage.get("cache_read_tokens", 0)
            total_cache_write += usage.get("cache_write_tokens", 0)
            model_calls += 1
        elif ed["event_type"] == "tool":
            event.update(data)
            tool_calls += 1
        elif ed["event_type"] in ("span_begin", "span_end"):
            event.update(data)
        elif ed["event_type"] in ("info", "error"):
            event.update(data)

        events.append(event)

    started = rd.get("started_at", "")
    completed = rd.get("completed_at")
    duration_ms = 0
    if started and completed:
        try:
            from datetime import datetime as dt
            t0 = dt.fromisoformat(started)
            t1 = dt.fromisoformat(completed)
            duration_ms = int((t1 - t0).total_seconds() * 1000)
        except (ValueError, TypeError):
            pass

    config = json.loads(rd.get("config", "{}"))

    conn.close()
    return {
        "id": run_id,
        "workspace_id": rd["workspace_id"],
        "workspace_name": rd.get("workspace_name", ""),
        "run_type": rd["run_type"],
        "status": rd["status"],
        "started_at": started,
        "completed_at": completed,
        "description": rd.get("description"),
        "scope_node_headline": rd.get("scope_node_headline"),
        "total_cost_usd": total_cost,
        "total_usage": {
            "input_tokens": total_input,
            "output_tokens": total_output,
            "cache_read_tokens": total_cache_read,
            "cache_write_tokens": total_cache_write,
        },
        "model_call_count": model_calls,
        "tool_call_count": tool_calls,
        "duration_ms": duration_ms,
        "config": config,
        "events": events,
    }


if __name__ == "__main__":
    uvicorn.run(
        "serve:app",
        host="0.0.0.0",
        port=8099,
        reload=True,
        reload_dirs=[str(Path(__file__).parent)],
    )
