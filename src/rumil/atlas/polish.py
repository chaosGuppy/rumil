"""Round-E polish: cross-call-type variance, in-flight queue.

Both endpoints exist to let an operator answer "where's the trouble
right now?" / "which call types are most unstable?" without a separate
SQL trip.
"""

from __future__ import annotations

from datetime import UTC, datetime

from rumil.atlas.aggregate import _events_of
from rumil.atlas.schemas import (
    CallTypeVariance,
    CallTypeVarianceSummary,
    InFlightCall,
    InFlightQueue,
)
from rumil.atlas.stats import (
    _calls_in_runs,
    _percentile,
    _recent_run_ids,
)
from rumil.database import DB


def _stdev(values: list[float], mean: float) -> float:
    if len(values) < 2:
        return 0.0
    sq = sum((v - mean) ** 2 for v in values)
    return (sq / (len(values) - 1)) ** 0.5


async def build_variance_summary(
    db: DB,
    project_id: str | None = None,
    n_runs: int = 100,
    min_invocations: int = 3,
) -> CallTypeVarianceSummary:
    """For every call type seen across recent runs, return mean / p50 /
    p99 / coefficient-of-variation cost. Useful for spotting "this call
    type is unstable" outliers without inspecting each /calls/{ct}/stats
    individually."""
    run_ids = await _recent_run_ids(db, project_id, n_runs)
    rows = await _calls_in_runs(db, run_ids)
    by_type: dict[str, list[float]] = {}
    for r in rows:
        ct = str(r.get("call_type") or "")
        if not ct:
            continue
        by_type.setdefault(ct, []).append(float(r.get("cost_usd") or 0.0))
    out: list[CallTypeVariance] = []
    for ct, costs in by_type.items():
        if len(costs) < min_invocations:
            continue
        mean = sum(costs) / len(costs)
        p50 = _percentile(costs, 50)
        p99 = _percentile(costs, 99)
        sd = _stdev(costs, mean)
        cv = round(sd / mean, 4) if mean > 0 else 0.0
        ratio = round(p99 / p50, 4) if p50 > 0 else None
        out.append(
            CallTypeVariance(
                call_type=ct,
                n_invocations=len(costs),
                mean_cost_usd=round(mean, 4),
                p50_cost_usd=p50,
                p99_cost_usd=p99,
                cv=cv,
                p99_p50_ratio=ratio,
            )
        )
    out.sort(key=lambda x: x.cv, reverse=True)
    return CallTypeVarianceSummary(rows=out, n_runs_scanned=len(run_ids))


async def build_in_flight_queue(
    db: DB,
    project_id: str | None = None,
    stuck_seconds: int = 300,
    limit: int = 100,
) -> InFlightQueue:
    """All calls currently pending/running across the workspace (or just
    a project), with stuck detection."""
    query = (
        db.client.table("calls")
        .select("id, call_type, status, run_id, project_id, created_at, trace_json")
        .in_("status", ["pending", "running"])
        .order("created_at", desc=True)
        .limit(limit)
    )
    if project_id:
        query = query.eq("project_id", project_id)
    res = await db._execute(query)
    rows = list(res.data or [])

    now = datetime.now(UTC)
    items: list[InFlightCall] = []
    n_running = 0
    n_pending = 0
    n_stuck = 0

    workflow_by_run: dict[str, str | None] = {}
    if rows:
        run_ids = list({str(r.get("run_id")) for r in rows if r.get("run_id")})
        if run_ids:
            res2 = await db._execute(
                db.client.table("runs").select("id, config").in_("id", run_ids)
            )
            for run in res2.data or []:
                cfg = run.get("config") or {}
                rid = str(run.get("id") or "")
                wf = cfg.get("workflow") or cfg.get("prioritizer_variant") or cfg.get("task_name")
                workflow_by_run[rid] = wf

    for c in rows:
        status = str(c.get("status") or "")
        if status == "running":
            n_running += 1
        elif status == "pending":
            n_pending += 1
        started_at = c.get("created_at")
        seconds_since_start: float | None = None
        if isinstance(started_at, str):
            try:
                started_dt = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
                if started_dt.tzinfo is None:
                    started_dt = started_dt.replace(tzinfo=UTC)
                seconds_since_start = (now - started_dt).total_seconds()
            except ValueError:
                pass

        last_event_ts: str | None = None
        seconds_since_last: float | None = None
        events = _events_of(c)
        for e in events:
            ts = e.get("ts")
            if isinstance(ts, str) and (last_event_ts is None or ts > last_event_ts):
                last_event_ts = ts
        if last_event_ts:
            try:
                last_dt = datetime.fromisoformat(last_event_ts.replace("Z", "+00:00"))
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=UTC)
                seconds_since_last = (now - last_dt).total_seconds()
            except ValueError:
                pass

        is_stuck = (seconds_since_last is not None and seconds_since_last >= stuck_seconds) or (
            seconds_since_last is None
            and seconds_since_start is not None
            and seconds_since_start >= stuck_seconds
        )
        if is_stuck:
            n_stuck += 1

        rid = str(c.get("run_id") or "")
        items.append(
            InFlightCall(
                call_id=str(c.get("id") or ""),
                call_type=str(c.get("call_type") or ""),
                run_id=rid,
                project_id=str(c.get("project_id") or ""),
                workflow_name=workflow_by_run.get(rid),
                status=status,
                started_at=started_at if isinstance(started_at, str) else None,
                last_event_ts=last_event_ts,
                seconds_since_start=seconds_since_start,
                seconds_since_last_event=seconds_since_last,
                is_stuck=is_stuck,
            )
        )
    items.sort(
        key=lambda i: (i.is_stuck, i.seconds_since_start or 0.0),
        reverse=True,
    )
    return InFlightQueue(
        items=items,
        n_running=n_running,
        n_pending=n_pending,
        n_stuck=n_stuck,
        stuck_threshold_seconds=stuck_seconds,
    )
