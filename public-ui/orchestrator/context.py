"""Composable branch context builder and tree helpers.

Context is built from layers — each layer is a function that returns a formatted
string section. Run types configure which layers they want.
"""

import json
import sqlite3
from collections.abc import Sequence


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


def get_branch_health(conn: sqlite3.Connection, node_id: str) -> dict:
    """Quick diagnostic of a branch's state."""
    tree = get_subtree(conn, node_id)
    stats: dict[str, int] = {
        "total": 0,
        "claims": 0,
        "hypotheses": 0,
        "evidence": 0,
        "uncertainties": 0,
        "questions": 0,
        "max_depth": 0,
        "leafs_without_content": 0,
        "no_credence": 0,
    }

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


def _layer_root(conn: sqlite3.Connection, scope_node_id: str, **kwargs: object) -> str:
    """Root node of the workspace — always included for orientation."""
    ancestors = get_ancestors(conn, scope_node_id)
    root = ancestors[-1] if ancestors else None
    if not root:
        row = conn.execute(
            "SELECT * FROM nodes WHERE id = ?", (scope_node_id,)
        ).fetchone()
        if row:
            root = dict(row)
    if not root:
        return ""
    return "# Root\n" + format_tree({"children": [], **root}, depth=0)


def _layer_ancestors(
    conn: sqlite3.Connection, scope_node_id: str, **kwargs: object
) -> str:
    """Ancestor chain from scope node to root, with sibling headlines at each level."""
    ancestors = get_ancestors(conn, scope_node_id)
    if not ancestors:
        return ""

    parts: list[str] = ["# Ancestor chain"]
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
                parts.append(
                    f"  [{sd['node_type']}] {sd['headline']} [{sd['id'][:8]}] L{sd['importance']}"
                )
            parts.append("")

    return "\n".join(parts)


def _layer_branch(
    conn: sqlite3.Connection,
    scope_node_id: str,
    *,
    max_importance: int = 3,
    max_content_chars: int = 300,
    max_content_depth: int = 4,
    **kwargs: object,
) -> str:
    """The scope node's subtree, filtered by importance level."""
    parts: list[str] = [f"# Scoped branch (filtered to importance <= {max_importance})"]

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
        parts.append(
            f"{indent}[{node.get('node_type', '?')}] "
            f"{node.get('headline', '?')} [{nid}] L{imp}{scores}"
        )
        if node.get("content") and depth < max_content_depth:
            parts.append(f"{indent}  {node['content'][:max_content_chars]}")
        for child in node.get("children", []):
            render_filtered(child, depth + 1)

    scope_tree = get_subtree(conn, scope_node_id)
    render_filtered(scope_tree)
    return "\n".join(parts)


def _layer_health(
    conn: sqlite3.Connection, scope_node_id: str, **kwargs: object
) -> str:
    """Branch health diagnostics as structured text."""
    health = get_branch_health(conn, scope_node_id)
    return "# Branch health\n" + json.dumps(health, indent=2)


def _layer_siblings(
    conn: sqlite3.Connection, scope_node_id: str, **kwargs: object
) -> str:
    """Sibling branches at the scope node's level (headlines only)."""
    row = conn.execute(
        "SELECT parent_id FROM nodes WHERE id = ?", (scope_node_id,)
    ).fetchone()
    if not row:
        return ""
    parent_id = dict(row).get("parent_id")
    if not parent_id:
        return ""
    siblings = conn.execute(
        "SELECT id, node_type, headline, importance FROM nodes "
        "WHERE parent_id = ? AND id != ? ORDER BY position",
        (parent_id, scope_node_id),
    ).fetchall()
    if not siblings:
        return ""
    parts = ["# Sibling branches"]
    for s in siblings:
        sd = dict(s)
        parts.append(
            f"  [{sd['node_type']}] {sd['headline']} [{sd['id'][:8]}] L{sd['importance']}"
        )
    return "\n".join(parts)


def _layer_pending(
    conn: sqlite3.Connection,
    scope_node_id: str,
    *,
    ws_id: str = "",
    **kwargs: object,
) -> str:
    """Pending suggestions relevant to this workspace."""
    if not ws_id:
        return ""
    pending = conn.execute(
        "SELECT * FROM suggestions WHERE workspace_id = ? AND status = 'pending' "
        "ORDER BY created_at LIMIT 10",
        (ws_id,),
    ).fetchall()
    if not pending:
        return ""
    parts = ["# Pending suggestions"]
    for s in pending:
        sd = dict(s)
        parts.append(
            f"  [{sd['id'][:8]}] {sd['suggestion_type']}: {sd.get('payload', '')[:100]}"
        )
    return "\n".join(parts)


def _layer_worldview(
    conn: sqlite3.Connection, scope_node_id: str, **kwargs: object
) -> str:
    """Surface the L0 band and promotion/demotion candidates for L-level reasoning."""
    tree = get_subtree(conn, scope_node_id)
    l0_nodes: list[dict] = []
    promote_candidates: list[dict] = []
    demote_candidates: list[dict] = []
    buried_uncertainties: list[dict] = []

    def scan(node: dict, depth: int = 0) -> None:
        imp = node.get("importance", 0)
        nt = node.get("node_type", "")
        cred = node.get("credence")
        rob = node.get("robustness")

        if imp == 0 and depth > 0:
            l0_nodes.append(node)
            if rob is not None and rob <= 2:
                demote_candidates.append(node)
        elif (
            imp >= 2 and cred is not None and rob is not None and cred >= 7 and rob >= 3
        ):
            promote_candidates.append(node)
        if nt == "uncertainty" and imp >= 2:
            buried_uncertainties.append(node)

        for child in node.get("children", []):
            scan(child, depth + 1)

    scan(tree)

    if not l0_nodes and not promote_candidates and not buried_uncertainties:
        return ""

    parts = ["# Worldview status (L-level review)"]

    if l0_nodes:
        parts.append("## Current L0 band")
        for n in l0_nodes:
            nid = n.get("id", "?")[:8]
            scores = ""
            if n.get("credence") is not None:
                scores += f" C{n['credence']}"
            if n.get("robustness") is not None:
                scores += f"/R{n['robustness']}"
            parts.append(
                f"  [{n.get('node_type', '?')}] {n.get('headline', '?')} [{nid}]{scores}"
            )

    if demote_candidates:
        parts.append("## Demotion candidates (L0 with low robustness)")
        for n in demote_candidates:
            nid = n.get("id", "?")[:8]
            parts.append(
                f"  [{nid}] {n.get('headline', '?')} — "
                f"R{n.get('robustness', '?')} at L0, is this earned?"
            )

    if promote_candidates:
        parts.append("## Promotion candidates (L1+ with high credence + robustness)")
        for n in promote_candidates:
            nid = n.get("id", "?")[:8]
            parts.append(
                f"  [{nid}] {n.get('headline', '?')} — "
                f"L{n.get('importance', '?')} C{n.get('credence', '?')}/R{n.get('robustness', '?')}"
            )

    if buried_uncertainties:
        parts.append(
            "## Buried uncertainties (L2+ uncertainties that may deserve higher importance)"
        )
        for n in buried_uncertainties:
            nid = n.get("id", "?")[:8]
            parts.append(
                f"  [{nid}] {n.get('headline', '?')} — L{n.get('importance', '?')}"
            )

    return "\n".join(parts)


def _suggestion_outcome(stype: str, payload: dict[str, object]) -> str:
    if stype == "add_to_branch":
        headline = payload.get("headline", "")
        ntype = payload.get("node_type", "claim")
        return f"created {ntype} node: {headline}" if headline else ""
    if stype == "relevel_node":
        new_imp = payload.get("new_importance")
        return f"importance set to L{new_imp}" if new_imp is not None else ""
    if stype == "resolve_tension":
        other = payload.get("other_node_id", "")
        return f"opposes link created with {str(other)[:8]}" if other else ""
    if stype == "merge_duplicate":
        keep = payload.get("keep_node_id", "")
        supersede = payload.get("supersede_node_id", "")
        if keep and supersede:
            return f"{str(supersede)[:8]} superseded by {str(keep)[:8]}"
    if stype == "cascade_review":
        changed_hl = payload.get("changed_headline", "")
        dep_hl = payload.get("dependent_headline", "")
        if changed_hl and dep_hl:
            return f"cascade reviewed: {dep_hl} depends on {changed_hl}"
    return ""


def _layer_history(
    conn: sqlite3.Connection, scope_node_id: str, *, ws_id: str = "", **kwargs: object
) -> str:
    """Recent run history and suggestion signals for this branch."""
    if not ws_id:
        return ""

    runs = conn.execute(
        "SELECT run_type, status, started_at, description FROM runs "
        "WHERE workspace_id = ? AND scope_node_id = ? "
        "ORDER BY started_at DESC LIMIT 5",
        (ws_id, scope_node_id),
    ).fetchall()

    accepted = conn.execute(
        "SELECT suggestion_type, payload FROM suggestions "
        "WHERE workspace_id = ? AND status = 'accepted' "
        "ORDER BY reviewed_at DESC LIMIT 5",
        (ws_id,),
    ).fetchall()

    rejected = conn.execute(
        "SELECT suggestion_type, payload FROM suggestions "
        "WHERE workspace_id = ? AND status = 'rejected' "
        "ORDER BY reviewed_at DESC LIMIT 5",
        (ws_id,),
    ).fetchall()

    if not runs and not accepted and not rejected:
        return ""

    parts = ["# Research history"]

    if runs:
        parts.append("## Recent runs on this branch")
        for r in runs:
            rd = dict(r)
            parts.append(
                f"  {rd.get('run_type', '?')} ({rd.get('status', '?')}) — {rd.get('description', '')[:80]}"
            )

    if accepted:
        parts.append("## Recently accepted suggestions (applied to workspace)")
        for s in accepted:
            sd = dict(s)
            payload = json.loads(sd.get("payload", "{}"))
            stype = sd.get("suggestion_type", "?")
            reasoning = payload.get("reasoning", "")[:80]
            detail = _suggestion_outcome(stype, payload)
            parts.append(f"  {stype}: {reasoning}")
            if detail:
                parts.append(f"    → {detail}")

    if rejected:
        parts.append("## Recently rejected suggestions (user disagreed)")
        for s in rejected:
            sd = dict(s)
            payload = json.loads(sd.get("payload", "{}"))
            parts.append(
                f"  {sd.get('suggestion_type', '?')}: {payload.get('reasoning', '')[:80]}"
            )

    return "\n".join(parts)


def _layer_all_branches_shallow(
    conn: sqlite3.Connection,
    scope_node_id: str,
    *,
    ws_id: str = "",
    **kwargs: object,
) -> str:
    """All top-level branches with their L0+L1 nodes — wide but shallow.

    Walks up to root from scope_node_id, then renders every direct child
    (branch head) with descendants filtered to importance <= 1. Also includes
    existing cross-branch links.
    """
    ancestors = get_ancestors(conn, scope_node_id)
    root_id = ancestors[-1]["id"] if ancestors else scope_node_id

    branches = conn.execute(
        "SELECT * FROM nodes WHERE parent_id = ? ORDER BY position",
        (root_id,),
    ).fetchall()
    if not branches:
        return ""

    parts: list[str] = ["# All branches (L0+L1 overview)"]

    for branch_row in branches:
        branch = dict(branch_row)
        bid = branch["id"][:8]
        scores = ""
        if branch.get("credence") is not None:
            scores += f" C{branch['credence']}"
        if branch.get("robustness") is not None:
            scores += f"/R{branch['robustness']}"
        parts.append(
            f"\n## [{branch.get('node_type', '?')}] "
            f"{branch.get('headline', '?')} [{bid}] L{branch.get('importance', 0)}{scores}"
        )
        if branch.get("content"):
            parts.append(f"  {branch['content'][:200]}")

        tree = get_subtree(conn, branch["id"])
        _render_important_descendants(tree, parts, max_importance=1)

    if ws_id:
        links = conn.execute(
            "SELECT source_id, target_id, link_type, strength, reasoning "
            "FROM node_links WHERE workspace_id = ? ORDER BY created_at",
            (ws_id,),
        ).fetchall()
        if links:
            link_node_ids = {
                dict(l)["source_id"] for l in links
            } | {dict(l)["target_id"] for l in links}
            placeholders = ",".join("?" for _ in link_node_ids)
            headline_rows = conn.execute(
                f"SELECT id, headline FROM nodes WHERE id IN ({placeholders})",
                list(link_node_ids),
            ).fetchall()
            headlines = {dict(r)["id"]: dict(r)["headline"] for r in headline_rows}

            parts.append("\n## Existing links")
            for link in links:
                ld = dict(link)
                src = ld["source_id"][:8]
                tgt = ld["target_id"][:8]
                src_hl = headlines.get(ld["source_id"], "?")
                tgt_hl = headlines.get(ld["target_id"], "?")
                strength = f" (strength {ld['strength']})" if ld.get("strength") else ""
                parts.append(
                    f"  {src_hl} [{src}] —[{ld.get('link_type', '?')}]→ "
                    f"{tgt_hl} [{tgt}]{strength}"
                )
                if ld.get("reasoning"):
                    parts.append(f"    {ld['reasoning'][:120]}")

    return "\n".join(parts)


def _render_important_descendants(
    node: dict, parts: list[str], *, max_importance: int = 1
) -> None:
    """Append descendants with importance <= max_importance to parts list."""
    for child in node.get("children", []):
        imp = child.get("importance", 0)
        if imp <= max_importance:
            nid = child.get("id", "?")[:8]
            scores = ""
            if child.get("credence") is not None:
                scores += f" C{child['credence']}"
            if child.get("robustness") is not None:
                scores += f"/R{child['robustness']}"
            parts.append(
                f"  [{child.get('node_type', '?')}] "
                f"{child.get('headline', '?')} [{nid}] L{imp}{scores}"
            )
        _render_important_descendants(child, parts, max_importance=max_importance)


def _layer_cascades(
    conn: sqlite3.Connection,
    scope_node_id: str,
    *,
    ws_id: str = "",
    **kwargs: object,
) -> str:
    """Surface pending cascade_review suggestions for nodes in this branch."""
    if not ws_id:
        return ""

    branch_ids: list[str] = []

    def collect_ids(node: dict) -> None:
        branch_ids.append(node.get("id", ""))
        for child in node.get("children", []):
            collect_ids(child)

    tree = get_subtree(conn, scope_node_id)
    collect_ids(tree)

    if not branch_ids:
        return ""

    placeholders = ",".join("?" for _ in branch_ids)
    cascades = conn.execute(
        "SELECT * FROM suggestions "
        f"WHERE workspace_id = ? AND suggestion_type = 'cascade_review' AND status = 'pending' "
        f"AND target_node_id IN ({placeholders}) "
        "ORDER BY created_at DESC LIMIT 10",
        [ws_id, *branch_ids],
    ).fetchall()

    if not cascades:
        return ""

    parts = [
        "# Cascade alerts",
        "Nodes in this branch depend on things that recently changed. Review whether "
        "the dependency still holds and adjust scores/levels if needed.",
        "",
    ]
    for row in cascades:
        sd = dict(row)
        payload = json.loads(sd.get("payload", "{}"))
        changed_hl = payload.get("changed_headline", "?")
        changed_id = payload.get("changed_node_id", "?")[:8]
        dep_hl = payload.get("dependent_headline", "?")
        dep_id = payload.get("dependent_node_id", "?")[:8]
        change_details = payload.get("changes", {})

        change_strs = []
        for field, vals in change_details.items():
            if field == "superseded":
                change_strs.append("judgement superseded")
            else:
                change_strs.append(f"{field}: {vals.get('old')}→{vals.get('new')}")

        parts.append(
            f"- **{dep_hl}** [{dep_id}] depends on **{changed_hl}** [{changed_id}]"
        )
        if change_strs:
            parts.append(f"  Changed: {', '.join(change_strs)}")
        parts.append("")

    return "\n".join(parts)


LAYER_BUILDERS = {
    "root": _layer_root,
    "ancestors": _layer_ancestors,
    "branch": _layer_branch,
    "health": _layer_health,
    "siblings": _layer_siblings,
    "pending": _layer_pending,
    "worldview": _layer_worldview,
    "history": _layer_history,
    "all_branches_shallow": _layer_all_branches_shallow,
    "cascades": _layer_cascades,
}


def build_branch_context(
    conn: sqlite3.Connection,
    scope_node_id: str,
    *,
    layers: Sequence[str] | None = None,
    max_importance: int = 3,
    ws_id: str = "",
) -> str:
    """Build branch-scoped context from composable layers.

    Available layers: root, ancestors, branch, health, siblings, pending.
    Default: root, ancestors, branch, health.
    """
    if layers is None:
        layers = ["root", "ancestors", "branch", "health"]
    parts = []
    for layer_name in layers:
        builder = LAYER_BUILDERS.get(layer_name)
        if not builder:
            continue
        part = builder(
            conn,
            scope_node_id,
            max_importance=max_importance,
            ws_id=ws_id,
        )
        if part:
            parts.append(part)
    return "\n\n".join(parts)


def _node_summary(node: dict) -> dict:
    """Extract a compact summary dict from a node row."""
    return {
        "id": node.get("id", "")[:8],
        "full_id": node.get("id", ""),
        "headline": node.get("headline", ""),
        "node_type": node.get("node_type", ""),
        "importance": node.get("importance", 0),
        "credence": node.get("credence"),
        "robustness": node.get("robustness"),
    }


def preview_branch_context(
    conn: sqlite3.Connection,
    scope_node_id: str,
    *,
    max_importance: int = 2,
    ws_id: str = "",
) -> dict:
    """Build a structured preview of what an orchestrator run would see.

    Returns dicts suitable for JSON serialization and frontend rendering.
    """
    scope_row = conn.execute(
        "SELECT * FROM nodes WHERE id = ?", (scope_node_id,)
    ).fetchone()
    if not scope_row:
        return {"error": "scope node not found"}
    scope_node = _node_summary(dict(scope_row))

    ancestors = get_ancestors(conn, scope_node_id)
    root = ancestors[-1] if ancestors else dict(scope_row)
    root_summary = _node_summary(root)

    context_nodes: list[dict] = []
    filtered_nodes: list[dict] = []

    tree = get_subtree(conn, scope_node_id)

    def collect(node: dict, depth: int = 0) -> None:
        imp = node.get("importance", 0)
        summary = {**_node_summary(node), "depth": depth, "layer": "branch"}
        if imp > max_importance and depth > 0:
            filtered_nodes.append(
                {
                    **summary,
                    "reason": f"importance L{imp} above threshold L{max_importance}",
                }
            )
        else:
            context_nodes.append(summary)
        for child in node.get("children", []):
            collect(child, depth + 1)

    collect(tree)

    scope_parent_id = dict(scope_row).get("parent_id")
    sibling_nodes: list[dict] = []
    if scope_parent_id:
        siblings = conn.execute(
            "SELECT * FROM nodes WHERE parent_id = ? AND id != ? ORDER BY position",
            (scope_parent_id, scope_node_id),
        ).fetchall()
        sibling_nodes = [_node_summary(dict(s)) for s in siblings]

    health = get_branch_health(conn, scope_node_id)

    return {
        "scope_node": scope_node,
        "root_node": root_summary,
        "context_nodes": context_nodes,
        "filtered_nodes": filtered_nodes,
        "sibling_nodes": sibling_nodes,
        "health": health,
    }
