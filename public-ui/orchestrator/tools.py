"""Orchestrator tool definitions, execution, and tool sets.

Tools are defined as dicts (Anthropic tool schema format) with rich behavioral
descriptions. Tool sets group tools by run type. The make_tool_executor factory
returns a closure that handles execution + action logging.
"""

import json
import sqlite3
from collections.abc import Callable, Sequence
from typing import Any

from orchestrator.context import build_branch_context, get_branch_health

HEADLINE_GUIDANCE = (
    "10-15 words (20-word ceiling). Must stand alone — a reader with no context "
    "should know what this node claims. Write like a newspaper headline: name the "
    "specific subject, include the key finding or caveat. Never use context-dependent "
    "language like 'this undercuts the premise' or 'key factor in the timeline'. "
    "Always name the specific subject — 'the election' is broken because it doesn't "
    "say which election."
)

CREDENCE_GUIDANCE = (
    "1-9 credence score. 1 = virtually impossible (<1%), 3 = unlikely (1-10%), "
    "5 = genuinely uncertain (30-70%), 7 = very likely (90-99%), "
    "9 = completely uncontroversial (>99.99%). Use even numbers to interpolate. "
    "These are all-things-considered probabilities — a claim can have strong evidence "
    "in its favor but still warrant only 6 if there are significant reasons for doubt."
)

ROBUSTNESS_GUIDANCE = (
    "1-5 robustness score, independent of credence. "
    "1 = wild guess (haven't investigated), 2 = informed impression (some evidence, "
    "could easily be missing something), 3 = considered view (moderate evidence, "
    "expect refinement not reversal), 4 = well-grounded (good evidence, multiple "
    "angles), 5 = highly robust (thoroughly tested, counterarguments well-mapped). "
    "You can have credence 7 with robustness 1 (confident but haven't checked) or "
    "credence 5 with robustness 4 (genuinely uncertain after thorough investigation)."
)

NODE_TYPE_GUIDANCE = (
    "claim = falsifiable assertion about the world. "
    "hypothesis = claim specifically being investigated/tested. "
    "evidence = concrete finding, data point, or source-backed observation. "
    "uncertainty = identified gap, tension, or open question within the branch. "
    "context = background/framing that helps interpret other nodes. "
    "question = research question that could spawn its own investigation. "
    "judgement = synthesized position on a branch, supersedes prior judgements on the same scope. "
    "concept = reusable definition or framework referenced across branches."
)

IMPORTANCE_GUIDANCE = (
    "L-level: 0 = most important (L0, core worldview), 1 = important supporting "
    "finding, 2 = relevant detail, 3 = supplementary, 4+ = deep supplementary. "
    "L0 nodes form 'the worldview' — the claims a reader should know first. "
    "Depth in the tree is structural; importance is editorial judgement."
)


ADD_NODE: dict[str, Any] = {
    "name": "add_node",
    "description": (
        "Add a new node to the current branch. This is a direct write — the node "
        "will be immediately visible in the worldview. Use when you've identified a "
        "gap: a missing claim, unsupported assertion that needs evidence, an "
        "unacknowledged uncertainty, or a question worth investigating. Every claim "
        "and hypothesis MUST have credence and robustness scores — if you're unsure "
        "what to set, that tells you the robustness should be 1-2."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "node_type": {
                "type": "string",
                "enum": ["claim", "hypothesis", "evidence", "uncertainty", "context", "question", "judgement", "concept"],
                "description": NODE_TYPE_GUIDANCE,
            },
            "headline": {
                "type": "string",
                "description": HEADLINE_GUIDANCE,
            },
            "content": {
                "type": "string",
                "description": (
                    "Detailed explanation. For claims: the reasoning and evidence "
                    "behind the assertion. For evidence: the specific finding and its "
                    "source. For uncertainties: what is unknown and why it matters. "
                    "Should be substantive — a node with only a headline and no content "
                    "is a placeholder, not a contribution."
                ),
            },
            "parent_id": {
                "type": "string",
                "description": "Short ID of the parent node in this branch.",
            },
            "importance": {
                "type": "integer",
                "description": IMPORTANCE_GUIDANCE,
            },
            "credence": {
                "type": "integer",
                "description": CREDENCE_GUIDANCE,
            },
            "robustness": {
                "type": "integer",
                "description": ROBUSTNESS_GUIDANCE,
            },
        },
        "required": ["node_type", "headline", "content", "parent_id"],
    },
}

UPDATE_NODE: dict[str, Any] = {
    "name": "update_node",
    "description": (
        "Update an existing node's content, credence, robustness, or headline. "
        "Use when the branch assessment reveals that an existing node's scores "
        "are misaligned with evidence, a headline is unclear or context-dependent, "
        "or content needs correction. Provide only the fields you want to change."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "node_id": {"type": "string", "description": "Short ID of the node to update."},
            "headline": {
                "type": "string",
                "description": HEADLINE_GUIDANCE,
            },
            "content": {
                "type": "string",
                "description": "Updated explanation. Should be a complete replacement, not a diff.",
            },
            "credence": {
                "type": "integer",
                "description": CREDENCE_GUIDANCE,
            },
            "robustness": {
                "type": "integer",
                "description": ROBUSTNESS_GUIDANCE,
            },
            "reasoning": {
                "type": "string",
                "description": "Why this update is warranted — what changed or what was wrong.",
            },
        },
        "required": ["node_id", "reasoning"],
    },
}

SUGGEST_CHANGE: dict[str, Any] = {
    "name": "suggest_change",
    "description": (
        "Suggest a change that affects another branch or requires human review. "
        "This is NOT applied immediately — it goes into a review queue. Use when "
        "you notice tensions between branches, duplicated findings, nodes that "
        "belong in a different branch, or cross-branch implications of your work. "
        "Good suggestions are specific and actionable; vague flags like 'this area "
        "needs more research' are noise."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "suggestion_type": {
                "type": "string",
                "enum": ["add_to_branch", "relevel_node", "resolve_tension", "merge_duplicate"],
                "description": (
                    "add_to_branch = suggest adding content to another branch. "
                    "relevel_node = suggest changing a node's importance in another branch. "
                    "resolve_tension = flag a contradiction between branches that needs resolution. "
                    "merge_duplicate = flag nodes across branches that say the same thing."
                ),
            },
            "target_node_id": {
                "type": "string",
                "description": "Short ID of the node this affects (in another branch).",
            },
            "reasoning": {
                "type": "string",
                "description": (
                    "Why this change should be made. Be specific: name the tension, "
                    "the duplication, or the gap. A reviewer seeing only this text "
                    "should understand the issue without needing to read the full branch."
                ),
            },
            "payload": {
                "type": "object",
                "description": "Details of the change (varies by type). For add_to_branch: include proposed node_type, headline, content. For relevel_node: include new_importance.",
            },
        },
        "required": ["suggestion_type", "target_node_id", "reasoning"],
    },
}

RELEVEL_NODE: dict[str, Any] = {
    "name": "relevel_node",
    "description": (
        "Change the importance level of a node in the current branch. Use when "
        "evidence has strengthened a finding (move toward L0) or weakened it "
        "(move toward L4+). L0 = core worldview finding, L1 = important supporting "
        "detail, L2 = relevant, L3 = supplementary, L4+ = deep supplementary. "
        "Be willing to demote — a node that seemed important during initial "
        "exploration may turn out to be peripheral."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "node_id": {"type": "string", "description": "Short ID of the node to relevel."},
            "new_importance": {
                "type": "integer",
                "description": "New L-level. 0 = most important (L0), higher = less important.",
            },
            "reasoning": {
                "type": "string",
                "description": "Why the importance changed — what evidence or assessment warrants this.",
            },
        },
        "required": ["node_id", "new_importance", "reasoning"],
    },
}

MOVE_NODE: dict[str, Any] = {
    "name": "move_node",
    "description": (
        "Move a node to a different parent within the current branch. Use when "
        "a node is structurally misplaced — it belongs under a different parent "
        "than where it currently sits. The node keeps all its children. Only works "
        "within the scoped branch."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "node_id": {"type": "string", "description": "Short ID of the node to move."},
            "new_parent_id": {"type": "string", "description": "Short ID of the new parent."},
            "reasoning": {"type": "string", "description": "Why this node belongs under the new parent."},
        },
        "required": ["node_id", "new_parent_id", "reasoning"],
    },
}

INSPECT_BRANCH: dict[str, Any] = {
    "name": "inspect_branch",
    "description": (
        "View the current branch's full context and health diagnostics. Always "
        "call this before making changes — understand the branch before acting. "
        "Returns the branch tree (filtered by importance), ancestor chain, sibling "
        "branches, and health stats (node counts, gaps, quality issues)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {},
    },
}

LINK_NODES: dict[str, Any] = {
    "name": "link_nodes",
    "description": (
        "Create a typed link between two nodes. Links express relationships "
        "beyond the tree structure — support, opposition, dependency, or "
        "association. Use when you notice that a node in this branch bears on "
        "a node elsewhere, or when you want to make a dependency explicit. "
        "Links are directional: source → target."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "source_id": {
                "type": "string",
                "description": "Short ID of the source node (the one making the claim or providing evidence).",
            },
            "target_id": {
                "type": "string",
                "description": "Short ID of the target node (the one being supported, opposed, or depended on).",
            },
            "link_type": {
                "type": "string",
                "enum": ["supports", "opposes", "depends_on", "related"],
                "description": (
                    "supports = source provides evidence or reasoning for target. "
                    "opposes = source provides evidence or reasoning against target. "
                    "depends_on = source's truth rests on target's truth (if target is wrong, source is in trouble). "
                    "related = weaker association, neither directionally supporting nor opposing."
                ),
            },
            "strength": {
                "type": "integer",
                "description": "1-5 how strongly this relationship holds. 1 = weak/tangential, 5 = decisive.",
            },
            "reasoning": {
                "type": "string",
                "description": "Why this link exists — what's the relationship?",
            },
        },
        "required": ["source_id", "target_id", "link_type", "reasoning"],
    },
}

ALL_TOOLS = [ADD_NODE, UPDATE_NODE, SUGGEST_CHANGE, RELEVEL_NODE, MOVE_NODE, LINK_NODES, INSPECT_BRANCH]

TOOL_SETS: dict[str, list[dict[str, Any]]] = {
    "explore": [ADD_NODE, UPDATE_NODE, RELEVEL_NODE, LINK_NODES, SUGGEST_CHANGE, INSPECT_BRANCH],
    "evaluate": [UPDATE_NODE, RELEVEL_NODE, LINK_NODES, SUGGEST_CHANGE, INSPECT_BRANCH],
    "restructure": [MOVE_NODE, RELEVEL_NODE, UPDATE_NODE, LINK_NODES, INSPECT_BRANCH],
}


def make_tool_executor(
    conn: sqlite3.Connection,
    ws_id: str,
    scope_node_id: str,
    run_id: str,
    *,
    dry_run: bool = False,
    resolve_node_id: Callable[[sqlite3.Connection, str], str | None] | None = None,
    new_id: Callable[[], str] | None = None,
    now_iso: Callable[[], str] | None = None,
) -> Callable[[str, dict], str]:
    """Create a tool executor closure for a specific orchestrator run.

    The resolve_node_id, new_id, and now_iso functions are injected from serve.py
    to avoid circular imports.
    """
    _resolve = resolve_node_id or (lambda conn, sid: None)
    _new_id = new_id or (lambda: __import__("uuid").uuid4().hex[:16])
    _now = now_iso or (lambda: __import__("datetime").datetime.now(__import__("datetime").UTC).isoformat())

    def execute(name: str, tool_input: dict) -> str:
        action_id = _new_id()
        prefix = "[dry-run] " if dry_run else ""

        if name == "add_node":
            output = _exec_add_node(conn, ws_id, tool_input, prefix, dry_run)
        elif name == "update_node":
            output = _exec_update_node(conn, tool_input, prefix, dry_run)
        elif name == "suggest_change":
            output = _exec_suggest_change(conn, ws_id, run_id, tool_input, prefix, dry_run)
        elif name == "relevel_node":
            output = _exec_relevel_node(conn, tool_input, prefix, dry_run)
        elif name == "move_node":
            output = _exec_move_node(conn, tool_input, prefix, dry_run)
        elif name == "link_nodes":
            output = _exec_link_nodes(conn, ws_id, tool_input, prefix, dry_run)
        elif name == "inspect_branch":
            output = _exec_inspect_branch(conn, scope_node_id, ws_id)
        else:
            output = f"Unknown tool: {name}"

        conn.execute(
            "INSERT INTO actions (id, run_id, action_type, input_data, output_data, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (action_id, run_id, f"orch:{name}", json.dumps(tool_input), output[:2000], _now()),
        )
        conn.commit()
        return output

    def _exec_add_node(
        conn: sqlite3.Connection, ws_id: str, inp: dict, prefix: str, dry: bool,
    ) -> str:
        parent_short = inp.get("parent_id", "")
        parent_full = _resolve(conn, parent_short)
        if not parent_full:
            return f"Parent node '{parent_short}' not found"
        if dry:
            return (
                f"{prefix}Would create [{inp.get('node_type', 'claim')}] "
                f"under {parent_short}: {inp['headline']}"
            )
        node_id = _new_id()
        conn.execute(
            "INSERT INTO nodes (id, workspace_id, parent_id, node_type, headline, content, "
            "credence, robustness, importance, position, created_at, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                node_id, ws_id, parent_full,
                inp.get("node_type", "claim"),
                inp["headline"],
                inp.get("content", ""),
                inp.get("credence"),
                inp.get("robustness"),
                inp.get("importance", 2),
                0, _now(), "orchestrator",
            ),
        )
        conn.commit()
        return f"Created [{inp.get('node_type', 'claim')}] node {node_id[:8]}: {inp['headline']}"

    def _exec_update_node(
        conn: sqlite3.Connection, inp: dict, prefix: str, dry: bool,
    ) -> str:
        node_short = inp.get("node_id", "")
        full_id = _resolve(conn, node_short)
        if not full_id:
            return f"Node '{node_short}' not found"

        updates: list[str] = []
        params: list[object] = []
        for field in ("headline", "content", "credence", "robustness"):
            if field in inp:
                updates.append(f"{field} = ?")
                params.append(inp[field])

        if not updates:
            return f"No fields to update on {node_short}"

        if dry:
            fields = ", ".join(f for f in ("headline", "content", "credence", "robustness") if f in inp)
            return f"{prefix}Would update {node_short} fields: {fields}. Reason: {inp.get('reasoning', '')[:100]}"

        params.append(full_id)
        conn.execute(f"UPDATE nodes SET {', '.join(updates)} WHERE id = ?", params)
        conn.commit()
        fields = ", ".join(f for f in ("headline", "content", "credence", "robustness") if f in inp)
        return f"Updated {node_short} ({fields})"

    def _exec_suggest_change(
        conn: sqlite3.Connection, ws_id: str, run_id: str, inp: dict, prefix: str, dry: bool,
    ) -> str:
        if dry:
            return (
                f"{prefix}Would queue suggestion: {inp.get('suggestion_type', '?')} "
                f"on {inp.get('target_node_id', '?')}: {inp.get('reasoning', '')[:100]}"
            )
        sug_id = _new_id()
        conn.execute(
            "INSERT INTO suggestions (id, workspace_id, run_id, suggestion_type, "
            "target_node_id, payload, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                sug_id, ws_id, run_id,
                inp.get("suggestion_type", ""),
                _resolve(conn, inp.get("target_node_id", "")) or "",
                json.dumps({"reasoning": inp.get("reasoning", ""), **inp.get("payload", {})}),
                _now(),
            ),
        )
        conn.commit()
        return (
            f"Queued suggestion {sug_id[:8]}: "
            f"{inp.get('suggestion_type', '?')} on {inp.get('target_node_id', '?')}"
        )

    def _exec_relevel_node(
        conn: sqlite3.Connection, inp: dict, prefix: str, dry: bool,
    ) -> str:
        node_short = inp.get("node_id", "")
        full_id = _resolve(conn, node_short)
        new_imp = inp.get("new_importance", 0)
        if not full_id:
            return f"Node '{node_short}' not found"
        if dry:
            return f"{prefix}Would relevel {node_short} to L{new_imp}: {inp.get('reasoning', '')[:100]}"
        conn.execute("UPDATE nodes SET importance = ? WHERE id = ?", (new_imp, full_id))
        conn.commit()
        return f"Releveled {node_short} to L{new_imp}"

    def _exec_move_node(
        conn: sqlite3.Connection, inp: dict, prefix: str, dry: bool,
    ) -> str:
        node_short = inp.get("node_id", "")
        new_parent_short = inp.get("new_parent_id", "")
        full_id = _resolve(conn, node_short)
        new_parent_full = _resolve(conn, new_parent_short)
        if not full_id:
            return f"Node '{node_short}' not found"
        if not new_parent_full:
            return f"New parent '{new_parent_short}' not found"
        if dry:
            return (
                f"{prefix}Would move {node_short} under {new_parent_short}: "
                f"{inp.get('reasoning', '')[:100]}"
            )
        conn.execute("UPDATE nodes SET parent_id = ? WHERE id = ?", (new_parent_full, full_id))
        conn.commit()
        return f"Moved {node_short} under {new_parent_short}"

    def _exec_link_nodes(
        conn: sqlite3.Connection, ws_id: str, inp: dict, prefix: str, dry: bool,
    ) -> str:
        source_short = inp.get("source_id", "")
        target_short = inp.get("target_id", "")
        source_full = _resolve(conn, source_short)
        target_full = _resolve(conn, target_short)
        if not source_full:
            return f"Source node '{source_short}' not found"
        if not target_full:
            return f"Target node '{target_short}' not found"
        link_type = inp.get("link_type", "related")
        if dry:
            return (
                f"{prefix}Would link {source_short} —[{link_type}]→ {target_short}: "
                f"{inp.get('reasoning', '')[:100]}"
            )
        link_id = _new_id()
        conn.execute(
            "INSERT INTO node_links (id, workspace_id, source_id, target_id, link_type, "
            "strength, reasoning, created_at, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                link_id, ws_id, source_full, target_full,
                link_type, inp.get("strength"), inp.get("reasoning", ""),
                _now(), "orchestrator",
            ),
        )
        conn.commit()
        return f"Linked {source_short} —[{link_type}]→ {target_short}"

    def _exec_inspect_branch(
        conn: sqlite3.Connection, scope_node_id: str, ws_id: str,
    ) -> str:
        context = build_branch_context(conn, scope_node_id, ws_id=ws_id)
        health = get_branch_health(conn, scope_node_id)
        return f"{context}\n\n# Branch health\n{json.dumps(health, indent=2)}"

    return execute
