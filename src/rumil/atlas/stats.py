"""Empirical stats per call type and per move.

Sibling of ``aggregate.py`` but keyed on registry items (a single
``CallType`` or ``MoveType``) rather than on a workflow. Walks the
calls table + their ``trace_json`` across recent runs to surface:

- per call type: how often this call ran, mean cost, mean rounds, mean
  pages loaded, status mix, top error excerpts, common move co-firings
- per move: how often this move was invoked across recent runs, which
  call types invoked it, last-seen timestamp, cost-per-invocation
  attribution

Cheap by default — caps at the most-recent 200 runs (configurable).
Reads from baseline (non-staged) data only.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Iterable, Sequence
from typing import Any

from rumil.atlas.schemas import (
    CallTypeInvocationCount,
    CallTypeStats,
    CoFiringCount,
    MoveCount,
    MoveStats,
)
from rumil.database import DB
from rumil.models import CallType

log = logging.getLogger(__name__)


async def _recent_run_ids(
    db: DB,
    project_id: str | None,
    limit: int,
) -> list[str]:
    query = (
        db.client.table("runs").select("id, created_at").order("created_at", desc=True).limit(limit)
    )
    if project_id:
        query = query.eq("project_id", project_id)
    res = await db._execute(query)
    return [str(r["id"]) for r in (res.data or []) if r.get("id")]


async def _calls_in_runs(
    db: DB,
    run_ids: Sequence[str],
    *,
    call_type: str | None = None,
) -> list[dict[str, Any]]:
    if not run_ids:
        return []
    query = (
        db.client.table("calls")
        .select(
            "id, call_type, status, cost_usd, created_at, completed_at, "
            "scope_page_id, run_id, trace_json"
        )
        .in_("run_id", list(run_ids))
    )
    if call_type:
        query = query.eq("call_type", call_type)
    res = await db._execute(query)
    return list(res.data or [])


def _events(call_row: dict[str, Any]) -> list[dict[str, Any]]:
    raw = call_row.get("trace_json") or []
    if isinstance(raw, list):
        return [e for e in raw if isinstance(e, dict)]
    return []


def _pages_loaded(events: Iterable[dict[str, Any]]) -> int:
    seen: set[str] = set()
    for e in events:
        if e.get("event") == "load_page":
            pid = e.get("page_id")
            if isinstance(pid, str):
                seen.add(pid)
        elif e.get("event") == "context_built":
            for key in (
                "working_context_page_ids",
                "preloaded_page_ids",
                "full_pages",
                "abstract_pages",
                "summary_pages",
                "distillation_pages",
                "scope_linked_pages",
            ):
                refs = e.get(key) or []
                for ref in refs:
                    pid = ref.get("page_id") if isinstance(ref, dict) else ref
                    if isinstance(pid, str):
                        seen.add(pid)
    return len(seen)


def _rounds_for_call(events: Iterable[dict[str, Any]]) -> int:
    """Count distinct round indices on llm_exchange events; falls back to 1."""
    rounds: set[int] = set()
    for e in events:
        if e.get("event") == "llm_exchange":
            r = e.get("round")
            if isinstance(r, int):
                rounds.add(r)
    return len(rounds) or 1


def _move_invocations(events: Iterable[dict[str, Any]]) -> Counter[str]:
    """Count how many times each move type fired in this call's trace.

    ``MoveTraceItem.type`` carries the move name (e.g. ``"create_claim"``) —
    the field is ``type``, not ``move_type``, despite ``MoveType`` being
    the enum name. Account for both spellings just in case anything
    historical wrote the alternative.
    """
    counts: Counter[str] = Counter()
    for e in events:
        if e.get("event") == "moves_executed":
            for m in e.get("moves") or []:
                if isinstance(m, dict):
                    mt = m.get("type") or m.get("move_type")
                    if isinstance(mt, str):
                        counts[mt] += 1
    return counts


def _error_excerpt(events: Iterable[dict[str, Any]]) -> str | None:
    for e in events:
        if e.get("event") == "error":
            msg = e.get("message")
            if isinstance(msg, str) and msg.strip():
                return msg[:240]
    return None


def _mean(values: Sequence[float]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0


async def build_call_type_stats(
    db: DB,
    call_type: CallType,
    project_id: str | None = None,
    n_runs: int = 50,
) -> CallTypeStats:
    run_ids = await _recent_run_ids(db, project_id, n_runs)
    rows = await _calls_in_runs(db, run_ids, call_type=call_type.value)

    n_invocations = len(rows)
    runs_seen = {str(r.get("run_id")) for r in rows if r.get("run_id")}
    costs = [float(r.get("cost_usd") or 0.0) for r in rows]
    pages = [_pages_loaded(_events(r)) for r in rows]
    rounds = [_rounds_for_call(_events(r)) for r in rows]
    statuses: Counter[str] = Counter(str(r.get("status") or "unknown") for r in rows)

    move_counts: Counter[str] = Counter()
    co_firing: Counter[tuple[str, str]] = Counter()
    error_excerpts: list[str] = []
    for r in rows:
        events = _events(r)
        invs = _move_invocations(events)
        move_counts.update(invs)
        moves_in_call = sorted(invs.keys())
        for i, a in enumerate(moves_in_call):
            for b in moves_in_call[i + 1 :]:
                co_firing[(a, b)] += 1
        err = _error_excerpt(events)
        if err:
            error_excerpts.append(err)

    return CallTypeStats(
        call_type=call_type.value,
        scanned_runs=len(run_ids),
        runs_with_call=len(runs_seen),
        n_invocations=n_invocations,
        mean_cost_usd=_mean(costs),
        total_cost_usd=round(sum(costs), 4),
        mean_pages_loaded=_mean(pages),
        mean_rounds=_mean(rounds),
        status_counts=dict(statuses),
        top_moves=[MoveCount(move_type=k, count=v) for k, v in move_counts.most_common(20)],
        top_co_firings=[
            CoFiringCount(a=a, b=b, count=v) for (a, b), v in co_firing.most_common(20)
        ],
        recent_errors=error_excerpts[:5],
    )


async def build_move_stats(
    db: DB,
    move_type: str,
    project_id: str | None = None,
    n_runs: int = 50,
) -> MoveStats:
    run_ids = await _recent_run_ids(db, project_id, n_runs)
    rows = await _calls_in_runs(db, run_ids)

    invocations_total = 0
    by_call_type: Counter[str] = Counter()
    runs_seen: set[str] = set()
    last_seen: str | None = None

    for r in rows:
        events = _events(r)
        n = sum(
            1
            for e in events
            if e.get("event") == "moves_executed"
            for m in (e.get("moves") or [])
            if isinstance(m, dict) and (m.get("type") or m.get("move_type")) == move_type
        )
        if n:
            invocations_total += n
            by_call_type[str(r.get("call_type") or "unknown")] += n
            rid = r.get("run_id")
            if isinstance(rid, str):
                runs_seen.add(rid)
            ts = r.get("completed_at") or r.get("created_at")
            if isinstance(ts, str) and (last_seen is None or ts > last_seen):
                last_seen = ts

    return MoveStats(
        move_type=move_type,
        scanned_runs=len(run_ids),
        runs_with_move=len(runs_seen),
        n_invocations=invocations_total,
        invocations_by_call_type=[
            CallTypeInvocationCount(call_type=ct, count=n) for ct, n in by_call_type.most_common(20)
        ],
        last_seen=last_seen,
    )
