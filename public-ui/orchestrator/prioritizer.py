"""Branch selection logic for the orchestrator.

Picks which branch to orchestrate next based on health diagnostics and
run history.
"""

import sqlite3

from orchestrator.context import get_branch_health


def pick_next_branch(conn: sqlite3.Connection, ws_id: str) -> str | None:
    """Pick the L0 branch with the worst health score.

    Scoring: lower = worse = prioritized for improvement.
    Factors: node count, evidence presence, missing credence, empty leaves.
    """
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
        score = (
            health["total"]
            + health["evidence"] * 2
            - health["no_credence"] * 3
            - health["leafs_without_content"] * 2
        )
        if score < worst_score:
            worst_score = score
            best_id = child_dict["id"]

    return best_id
