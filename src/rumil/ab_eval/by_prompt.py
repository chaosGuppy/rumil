"""Group runs by primary_prompt_hash and pair them for A/B comparison.

Phase 4 of the prompt-versioning refactor: once prompts are
content-addressed and every call stamps
``calls.primary_prompt_hash`` / ``primary_prompt_name``, the natural
question is "did prompt version X outperform Y on this call type?".
This module turns that question into a set of paired run IDs that
``run_ab_eval`` can evaluate.

Today we just do the pairing + selection. Wiring a
``compare_prompt_versions`` CLI entry point is a follow-up — the
tricky pieces (pair matching, min_runs filtering, project scoping)
live here so both CLI + a future parma page can call the same helper.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from rumil.database import DB


@dataclass(frozen=True)
class PromptVersionPair:
    """One (run_a, run_b) pair of runs to evaluate, plus the shared scope."""

    run_id_a: str
    run_id_b: str
    question_id: str
    call_type: str


async def select_pairs_by_prompt_hash(
    db: DB,
    *,
    prompt_name: str,
    hash_a: str,
    hash_b: str,
    call_type: str | None = None,
    max_pairs: int = 10,
) -> list[PromptVersionPair]:
    """Find calls with each prompt hash, pair by question, return up to ``max_pairs``.

    Strategy: query both halves of calls tagged with the two hashes,
    filter to a call_type if supplied, group by ``scope_page_id``, and
    emit pairs where each side has at least one candidate (preferring
    the most recent on each side). We match on question scope rather
    than task_shape for simplicity — a task_shape-aware matcher is a
    follow-up.

    Respects ``db.project_id`` when set: runs from other projects are
    filtered out. The calls table has no ``staged`` column today so
    staged A/B contamination is not a concern here; the runs themselves
    are scoped via ``calls.run_id`` which already knows about staging.
    """
    if not prompt_name or not hash_a or not hash_b:
        raise ValueError("prompt_name, hash_a, hash_b are all required")
    if hash_a == hash_b:
        raise ValueError(f"hash_a == hash_b ({hash_a!r}) — nothing to compare")

    a_rows = await _fetch_calls(db, prompt_name, hash_a, call_type)
    b_rows = await _fetch_calls(db, prompt_name, hash_b, call_type)

    a_by_q: dict[str, list[dict[str, Any]]] = {}
    for row in a_rows:
        qid = row.get("scope_page_id")
        if not qid:
            continue
        a_by_q.setdefault(qid, []).append(row)

    b_by_q: dict[str, list[dict[str, Any]]] = {}
    for row in b_rows:
        qid = row.get("scope_page_id")
        if not qid:
            continue
        b_by_q.setdefault(qid, []).append(row)

    shared = sorted(a_by_q.keys() & b_by_q.keys())
    pairs: list[PromptVersionPair] = []
    for qid in shared:
        a_latest = a_by_q[qid][0]
        b_latest = b_by_q[qid][0]
        if not (a_latest.get("run_id") and b_latest.get("run_id")):
            continue
        pairs.append(
            PromptVersionPair(
                run_id_a=a_latest["run_id"],
                run_id_b=b_latest["run_id"],
                question_id=qid,
                call_type=a_latest.get("call_type") or "",
            )
        )
        if len(pairs) >= max_pairs:
            break
    return pairs


async def _fetch_calls(
    db: DB,
    prompt_name: str,
    prompt_hash: str,
    call_type: str | None,
) -> list[dict[str, Any]]:
    q = (
        db.client.table("calls")
        .select("id,run_id,call_type,scope_page_id,created_at,primary_prompt_hash")
        .eq("primary_prompt_name", prompt_name)
        .eq("primary_prompt_hash", prompt_hash)
        .order("created_at", desc=True)
    )
    if db.project_id:
        q = q.eq("project_id", db.project_id)
    if call_type is not None:
        q = q.eq("call_type", call_type)
    result = await db._execute(q)
    return list(result.data or [])


async def list_prompt_versions(
    db: DB,
    *,
    prompt_name: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Return prompt_versions rows for browsing/UIs, newest-first.

    Useful for a "what prompts exist for ``find_considerations``?"
    dropdown in the trace UI or parma. Does not respect project
    scoping — prompts are shared across projects.
    """
    q = (
        db.client.table("prompt_versions")
        .select("hash,name,kind,first_seen_at,last_seen_at,seen_count")
        .order("first_seen_at", desc=True)
        .limit(limit)
    )
    if prompt_name is not None:
        q = q.eq("name", prompt_name)
    result = await db._execute(q)
    return list(result.data or [])


__all__ = [
    "PromptVersionPair",
    "list_prompt_versions",
    "select_pairs_by_prompt_hash",
]
