"""Per-call trace-event dumps + closing-review outcome classification.

The forensics layer flagged by the round-2 ergonomics + issue mining
passes: atlas was treating ``calls.status="complete"`` as authoritative
when in fact a call could complete with an ``ErrorEvent`` swallowed in
its ``trace_json``. This module exposes the raw trace event stream for
a single call (so operators can spot the silent failures), and
classifies each call's terminal state with a closing-review outcome
label (``completed`` / ``swallowed_error`` / ``noop`` / ``running``).
"""

from __future__ import annotations

from typing import Any

from rumil.atlas.schemas import CallEventDump, TraceEventRecord
from rumil.database import DB


def closing_review_outcome(call_row: dict[str, Any]) -> str | None:
    """Classify a call's terminal state for the forensics UI.

    - ``completed``: status=complete, no error events, ≥1 LLM exchange
    - ``swallowed_error``: status=complete BUT ≥1 error event in trace
    - ``noop``: status=complete with cost=0 and no LLM exchange
    - ``running``: status in (pending, running)
    - ``failed``: status=failed
    - ``unknown``: anything else / no signal
    """
    status = str(call_row.get("status") or "").lower()
    if status in {"pending", "running"}:
        return "running"
    if status == "failed":
        return "failed"
    events = call_row.get("trace_json") or []
    if not isinstance(events, list):
        events = []
    n_errors = sum(1 for e in events if isinstance(e, dict) and e.get("event") == "error")
    n_llm = sum(1 for e in events if isinstance(e, dict) and e.get("event") == "llm_exchange")
    cost = float(call_row.get("cost_usd") or 0.0)
    if status == "complete":
        if n_errors:
            return "swallowed_error"
        if n_llm == 0 and cost == 0.0:
            return "noop"
        return "completed"
    if not status:
        return None
    return status


async def build_call_event_dump(db: DB, call_id: str) -> CallEventDump | None:
    res = await db._execute(
        db.client.table("calls")
        .select("id, call_type, status, cost_usd, trace_json")
        .eq("id", call_id)
        .limit(1)
    )
    rows = list(res.data or [])
    if not rows:
        return None
    row = rows[0]
    raw_events = row.get("trace_json") or []
    if not isinstance(raw_events, list):
        raw_events = []

    events: list[TraceEventRecord] = []
    n_errors = 0
    n_llm = 0
    for i, e in enumerate(raw_events):
        if not isinstance(e, dict):
            continue
        kind = str(e.get("event") or "")
        if kind == "error":
            n_errors += 1
        elif kind == "llm_exchange":
            n_llm += 1
        payload = {k: v for k, v in e.items() if k != "event"}
        events.append(TraceEventRecord(index=i, kind=kind, payload=payload))

    return CallEventDump(
        call_id=str(row.get("id") or ""),
        call_type=str(row.get("call_type") or ""),
        status=str(row.get("status") or ""),
        n_events=len(events),
        events=events,
        n_error_events=n_errors,
        n_llm_exchanges=n_llm,
        closing_review_outcome=closing_review_outcome(row),
    )
