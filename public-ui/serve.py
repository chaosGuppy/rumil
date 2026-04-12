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
            position INTEGER NOT NULL DEFAULT 0,
            source_ids TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            created_by TEXT NOT NULL DEFAULT 'system'
        );

        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(id),
            started_at TEXT NOT NULL,
            completed_at TEXT,
            status TEXT NOT NULL DEFAULT 'running',
            description TEXT
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
        CREATE INDEX IF NOT EXISTS idx_runs_workspace ON runs(workspace_id);
        CREATE INDEX IF NOT EXISTS idx_actions_run ON actions(run_id);
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
) -> None:
    for pos, node in enumerate(nodes_data):
        node_id = new_id()
        conn.execute(
            "INSERT INTO nodes (id, workspace_id, parent_id, node_type, headline, content, "
            "credence, robustness, position, source_ids, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                node_id, ws_id, parent_id,
                node.get("node_type", "claim"),
                node.get("headline", ""),
                node.get("content", ""),
                node.get("credence"),
                node.get("robustness"),
                pos,
                json.dumps(node.get("source_page_ids", [])),
                now_iso(),
            ),
        )
        children = node.get("children", [])
        if children:
            _seed_nodes_recursive(conn, ws_id, node_id, children)


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

    else:
        output = f"Unknown tool: {name}"

    conn.execute(
        "INSERT INTO actions (id, run_id, action_type, input_data, output_data, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (action_id, run_id, name, json.dumps(tool_input), output[:2000], now_iso()),
    )
    conn.commit()
    return output


class ChatRequest(BaseModel):
    question_id: str | None = None
    messages: list[dict[str, Any]]
    workspace: str = "default"


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

    client = anthropic.AsyncAnthropic(api_key=api_key)
    messages = list(request.messages)
    tool_uses_log: list[ToolUseInfo] = []

    for _ in range(10):
        response = await client.messages.create(
            model="claude-sonnet-4-20250514",
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
