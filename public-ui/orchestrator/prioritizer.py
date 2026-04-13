"""Branch selection logic for the orchestrator.

Picks which branch to orchestrate next based on health diagnostics,
staleness, run history, and pending suggestions.
"""

import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime

from orchestrator.context import get_branch_health


def get_branch_run_history(
    conn: sqlite3.Connection,
    ws_id: str,
    branch_id: str,
) -> dict:
    """Get run history summary for a branch.

    Returns dict with:
        last_explore: ISO timestamp or None
        last_evaluate: ISO timestamp or None
        total_runs: int
        pending_suggestions: int
    """
    runs = conn.execute(
        "SELECT config, started_at FROM runs "
        "WHERE workspace_id = ? AND scope_node_id = ? AND status = 'completed' "
        "ORDER BY started_at DESC LIMIT 20",
        (ws_id, branch_id),
    ).fetchall()

    last_explore = None
    last_evaluate = None
    for r in runs:
        rd = dict(r)
        import json

        config = json.loads(rd.get("config") or "{}")
        run_type = config.get("run_type", "")
        ts = rd.get("started_at")
        if run_type == "explore" and not last_explore:
            last_explore = ts
        elif run_type == "evaluate" and not last_evaluate:
            last_evaluate = ts

    pending = conn.execute(
        "SELECT COUNT(*) FROM suggestions "
        "WHERE workspace_id = ? AND status = 'pending' AND target_node_id = ?",
        (ws_id, branch_id),
    ).fetchone()
    pending_count = pending[0] if pending else 0

    return {
        "last_explore": last_explore,
        "last_evaluate": last_evaluate,
        "total_runs": len(runs),
        "pending_suggestions": pending_count,
    }


def _hours_since(iso_ts: str | None) -> float:
    """Hours elapsed since an ISO timestamp. Returns inf if None."""
    if not iso_ts:
        return float("inf")
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return (datetime.now(UTC) - dt).total_seconds() / 3600
    except (ValueError, TypeError):
        return float("inf")


def has_judgement(conn: sqlite3.Connection, branch_id: str) -> bool:
    """Check whether a branch has a non-superseded judgement node."""
    row = conn.execute(
        "SELECT 1 FROM nodes WHERE parent_id = ? AND node_type = 'judgement' "
        "AND superseded_by IS NULL LIMIT 1",
        (branch_id,),
    ).fetchone()
    return row is not None


def has_siblings(conn: sqlite3.Connection, branch_id: str) -> bool:
    """Check whether a branch has sibling nodes (same parent)."""
    parent = conn.execute(
        "SELECT parent_id FROM nodes WHERE id = ?", (branch_id,)
    ).fetchone()
    if not parent:
        return False
    parent_id = dict(parent).get("parent_id")
    if not parent_id:
        return False
    row = conn.execute(
        "SELECT 1 FROM nodes WHERE parent_id = ? AND id != ? LIMIT 1",
        (parent_id, branch_id),
    ).fetchone()
    return row is not None


def has_cascade_suggestions(
    conn: sqlite3.Connection, ws_id: str, branch_id: str
) -> bool:
    """Check whether a branch has pending suggestions targeting it."""
    row = conn.execute(
        "SELECT 1 FROM suggestions "
        "WHERE workspace_id = ? AND status = 'pending' AND target_node_id = ? LIMIT 1",
        (ws_id, branch_id),
    ).fetchone()
    return row is not None


def _count_auto_explore_per_branch(
    conn: sqlite3.Connection,
    ws_id: str,
    root_id: str,
) -> dict[str, int]:
    """Count pending auto_explore suggestions per L0 branch.

    Uses a recursive CTE to walk each suggestion's target node up to its
    L0 branch ancestor (direct child of root).
    """
    rows = conn.execute(
        "WITH RECURSIVE ancestors AS ("
        "  SELECT s.id AS sug_id, n.id AS node_id, n.parent_id "
        "  FROM suggestions s "
        "  JOIN nodes n ON s.target_node_id = n.id "
        "  WHERE s.workspace_id = ? AND s.status = 'pending' "
        "    AND s.suggestion_type = 'auto_explore' "
        "  UNION ALL "
        "  SELECT a.sug_id, p.id, p.parent_id "
        "  FROM ancestors a "
        "  JOIN nodes p ON a.parent_id = p.id "
        "  WHERE p.parent_id IS NOT NULL"
        ") "
        "SELECT node_id, COUNT(DISTINCT sug_id) AS cnt "
        "FROM ancestors WHERE parent_id = ? "
        "GROUP BY node_id",
        (ws_id, root_id),
    ).fetchall()
    return {dict(r)["node_id"]: dict(r)["cnt"] for r in rows}


def pick_next_branch(
    conn: sqlite3.Connection,
    ws_id: str,
    *,
    exclude: Sequence[str] = (),
) -> str | None:
    """Pick the L0 branch most in need of attention.

    Scoring combines health deficits, staleness, and pending suggestions.
    Lower composite score = higher priority.

    exclude: branch IDs to skip (e.g. already visited in this loop).
    """
    root = conn.execute(
        "SELECT id FROM nodes WHERE workspace_id = ? AND parent_id IS NULL LIMIT 1",
        (ws_id,),
    ).fetchone()
    if not root:
        return None

    exclude_set = set(exclude)
    children = conn.execute(
        "SELECT id, headline FROM nodes WHERE parent_id = ? ORDER BY position",
        (dict(root)["id"],),
    ).fetchall()

    root_id = dict(root)["id"]
    auto_explore_by_branch = _count_auto_explore_per_branch(conn, ws_id, root_id)

    best_id = None
    worst_score = float("inf")
    for child in children:
        child_dict = dict(child)
        cid = child_dict["id"]
        if cid in exclude_set:
            continue

        health = get_branch_health(conn, cid)
        health_score = (
            health["total"]
            + health["evidence"] * 2
            - health["no_credence"] * 3
            - health["leafs_without_content"] * 2
        )

        history = get_branch_run_history(conn, ws_id, cid)
        staleness_hours = min(
            _hours_since(history["last_explore"]),
            _hours_since(history["last_evaluate"]),
        )
        staleness_bonus = -min(staleness_hours, 48) * 0.5

        suggestion_bonus = -history["pending_suggestions"] * 2
        auto_explore_bonus = -auto_explore_by_branch.get(cid, 0) * 5

        score = health_score + staleness_bonus + suggestion_bonus + auto_explore_bonus
        if score < worst_score:
            worst_score = score
            best_id = cid

    return best_id
