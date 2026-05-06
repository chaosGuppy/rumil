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
from datetime import datetime, timedelta
from typing import Any

from rumil.atlas.schemas import (
    CallTypeInvocationCount,
    CallTypeStats,
    CoFiringCount,
    HistogramBin,
    MoveCount,
    MoveStats,
    PathologyCounts,
    StatsBucket,
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
    chunk: int = 100,
) -> list[dict[str, Any]]:
    """Fetch calls across many runs, chunked to dodge postgrest's URL
    length limit (HTTP 414 once ~250 run_ids land in a single ``in_``).
    """
    if not run_ids:
        return []
    out: list[dict[str, Any]] = []
    ids = list(run_ids)
    for i in range(0, len(ids), chunk):
        batch = ids[i : i + chunk]
        query = (
            db.client.table("calls")
            .select(
                "id, call_type, status, cost_usd, created_at, completed_at, "
                "scope_page_id, run_id, trace_json"
            )
            .in_("run_id", batch)
        )
        if call_type:
            query = query.eq("call_type", call_type)
        res = await db._execute(query)
        out.extend(list(res.data or []))
    return out


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


def _percentile(values: Sequence[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return round(s[0], 4)
    k = (len(s) - 1) * (pct / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    frac = k - lo
    return round(s[lo] * (1 - frac) + s[hi] * frac, 4)


def _histogram_bins(
    values: Sequence[float],
    *,
    edges: Sequence[float] | None = None,
    integer: bool = False,
) -> list[HistogramBin]:
    if not values:
        return []
    if edges is None:
        v_max = max(values)
        if integer:
            top = max(int(v_max) + 1, 5)
            n_bins = min(top, 10)
            step = max(1, top // n_bins)
            edges = [float(i * step) for i in range(n_bins + 1)]
            if edges[-1] < v_max:
                edges = [*edges, float(int(v_max) + 1)]
        else:
            v_max = max(v_max, 0.001)
            edges = [v_max * (i / 8) for i in range(9)]
    out: list[HistogramBin] = []
    for i in range(len(edges) - 1):
        lo = edges[i]
        hi = edges[i + 1]
        last = i == len(edges) - 2
        count = sum(1 for v in values if lo <= v < hi or (last and v == hi))
        if integer:
            label = f"{int(lo)}–{int(hi)}"
        elif lo == 0:
            label = f"≤{hi:.2f}"
        else:
            label = f"{lo:.2f}–{hi:.2f}"
        out.append(HistogramBin(label=label, lo=lo, hi=hi, count=count))
    return out


def _bucket_label(bucket: str) -> tuple[str, int]:
    """Return (truncation format, days-per-bucket) for a bucket name."""
    if bucket == "week":
        return "%Y-%m-%d", 7
    if bucket == "month":
        return "%Y-%m", 30
    return "%Y-%m-%d", 1


def _floor_bucket(ts: datetime, bucket: str) -> datetime:
    if bucket == "week":
        # ISO week start (Monday)
        monday = ts - timedelta(days=ts.weekday())
        return monday.replace(hour=0, minute=0, second=0, microsecond=0)
    if bucket == "month":
        return ts.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return ts.replace(hour=0, minute=0, second=0, microsecond=0)


def _next_bucket(ts: datetime, bucket: str) -> datetime:
    if bucket == "week":
        return ts + timedelta(days=7)
    if bucket == "month":
        # crude: 32 days then floor
        nxt = ts + timedelta(days=32)
        return _floor_bucket(nxt, "month")
    return ts + timedelta(days=1)


def _build_series(
    rows: Sequence[dict[str, Any]],
    bucket: str,
) -> list[StatsBucket]:
    if not rows:
        return []
    buckets: dict[datetime, list[dict[str, Any]]] = {}
    for r in rows:
        ts_str = r.get("created_at")
        if not isinstance(ts_str, str):
            continue
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except ValueError:
            continue
        b = _floor_bucket(ts, bucket)
        buckets.setdefault(b, []).append(r)
    if not buckets:
        return []
    start = min(buckets.keys())
    end = _floor_bucket(datetime.now(start.tzinfo), bucket)
    out: list[StatsBucket] = []
    cursor = start
    while cursor <= end:
        bucket_rows = buckets.get(cursor, [])
        nxt = _next_bucket(cursor, bucket)
        costs = [float(r.get("cost_usd") or 0.0) for r in bucket_rows]
        rounds = [_rounds_for_call(_events(r)) for r in bucket_rows]
        out.append(
            StatsBucket(
                bucket_start=cursor.isoformat(),
                bucket_end=nxt.isoformat(),
                n_invocations=len(bucket_rows),
                mean_cost_usd=_mean(costs),
                total_cost_usd=round(sum(costs), 4),
                mean_rounds=_mean(rounds),
            )
        )
        cursor = nxt
        if len(out) >= 60:
            break
    return out


async def build_call_type_stats(
    db: DB,
    call_type: CallType,
    project_id: str | None = None,
    n_runs: int = 50,
    since: str | None = None,
    until: str | None = None,
    bucket: str | None = None,
) -> CallTypeStats:
    run_ids = await _recent_run_ids(db, project_id, n_runs)
    rows = await _calls_in_runs(db, run_ids, call_type=call_type.value)
    if since:
        rows = [r for r in rows if (r.get("created_at") or "") >= since]
    if until:
        rows = [r for r in rows if (r.get("created_at") or "") < until]

    n_invocations = len(rows)
    runs_seen = {str(r.get("run_id")) for r in rows if r.get("run_id")}
    costs = [float(r.get("cost_usd") or 0.0) for r in rows]
    pages = [_pages_loaded(_events(r)) for r in rows]
    rounds = [_rounds_for_call(_events(r)) for r in rows]
    statuses: Counter[str] = Counter(str(r.get("status") or "unknown") for r in rows)

    move_counts: Counter[str] = Counter()
    co_firing: Counter[tuple[str, str]] = Counter()
    error_excerpts: list[str] = []
    n_with_error = 0
    n_lying_complete = 0
    n_with_truncation = 0
    n_with_parse_fail = 0
    n_rounds_capped = 0
    n_error_events_total = 0
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
        # Pathology dims.
        had_error = False
        had_truncation = False
        had_parse_fail = False
        had_rounds_capped = False
        for e in events:
            et = e.get("event")
            if et == "error":
                had_error = True
                n_error_events_total += 1
                msg = str(e.get("message") or "").lower()
                if (
                    "max_tokens" in msg
                    or "truncat" in msg
                    or ("context" in msg and "exceed" in msg)
                ):
                    had_truncation = True
                if "json" in msg or "parse" in msg or "validation" in msg:
                    had_parse_fail = True
            elif et == "warning":
                msg = str(e.get("message") or "").lower()
                if "truncat" in msg or "max_tokens" in msg:
                    had_truncation = True
            elif et == "review_complete":
                # surface "rounds at cap" via the trace's remaining_fruit
                # signal? not directly available; skip for now.
                pass
            elif et == "llm_exchange" and e.get("error"):
                had_error = True
                msg = str(e.get("error") or "").lower()
                if "json" in msg or "parse" in msg:
                    had_parse_fail = True
                if "max_tokens" in msg or "truncat" in msg:
                    had_truncation = True
        if had_error:
            n_with_error += 1
            if str(r.get("status") or "").lower() == "complete":
                n_lying_complete += 1
        if had_truncation:
            n_with_truncation += 1
        if had_parse_fail:
            n_with_parse_fail += 1
        # rounds == max_rounds heuristic: rounds count from this trace
        # equals or exceeds 5 (the typical default cap) — best-effort.
        if _rounds_for_call(events) >= 5:
            had_rounds_capped = True
        if had_rounds_capped:
            n_rounds_capped += 1

    n = max(n_invocations, 1)
    pathology = PathologyCounts(
        n_error_events=n_error_events_total,
        error_pct=round(100.0 * n_with_error / n, 2),
        lying_complete_pct=round(100.0 * n_lying_complete / n, 2),
        rounds_capped_pct=round(100.0 * n_rounds_capped / n, 2),
        parse_fail_pct=round(100.0 * n_with_parse_fail / n, 2),
        truncated_pct=round(100.0 * n_with_truncation / n, 2),
    )

    series = _build_series(rows, bucket) if bucket else []
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
        p50_cost_usd=_percentile(costs, 50),
        p90_cost_usd=_percentile(costs, 90),
        p99_cost_usd=_percentile(costs, 99),
        rounds_histogram=_histogram_bins([float(r) for r in rounds], integer=True),
        cost_histogram=_histogram_bins(costs),
        pages_loaded_histogram=_histogram_bins([float(p) for p in pages], integer=True),
        series=series,
        bucket=bucket,
        since=since,
        until=until,
        top_moves=[MoveCount(move_type=k, count=v) for k, v in move_counts.most_common(20)],
        top_co_firings=[
            CoFiringCount(a=a, b=b, count=v) for (a, b), v in co_firing.most_common(20)
        ],
        recent_errors=error_excerpts[:5],
        pathology=pathology,
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
