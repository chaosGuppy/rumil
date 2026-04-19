"""Cheap heuristic confusion scan emitted as a reputation event.

Runs at the tail end of every completed call — zero LLM cost, based on
trace events and call metadata that are already in memory. The score is
in ``[-1, 1]`` where positive = no signs of confusion; negative = the
heuristics fired.

This is the "log-only" signal in Phase 1 of the evals-as-feedback
refactor — it populates ``reputation_events`` with
``source='confusion_scan'`` / ``dimension='confusion'`` so later phases
(prioritization context surfacing, EvalFeedbackPolicy) can read it
without paying for a full eval agent.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from rumil.database import DB
from rumil.models import Call, CallStatus
from rumil.tracing.tracer import CallTrace

log = logging.getLogger(__name__)

HEURISTIC_RESPONSE_SHORT_THRESHOLD = 200
HEURISTIC_INPUT_LARGE_THRESHOLD = 2000
COST_OUTLIER_MULTIPLIER = 3.0

_SIGNAL_WEIGHT = {
    "non_complete_status": 0.5,
    "trace_error": 0.5,
    "exchange_error": 0.4,
    "cost_outlier": 0.15,
    "thin_output": 0.15,
    "no_fruit": 0.1,
    "multiple_warnings": 0.15,
}


@dataclass
class ConfusionSignal:
    name: str
    weight: float
    detail: str


def _event_name(event: object) -> str:
    """Extract the event discriminator from a pydantic event or dict."""
    if isinstance(event, dict):
        return str(event.get("event", ""))
    name = getattr(event, "event", None)
    if name is None:
        return ""
    return str(name)


def _event_field(event: object, field: str) -> object:
    if isinstance(event, dict):
        return event.get(field)
    return getattr(event, field, None)


def compute_confusion_signals(
    *,
    call: Call,
    trace_events: Sequence[Any],
    cost_usd: float | None,
    cost_median: float | None = None,
    exchange_errors: int = 0,
    has_any_fruit: bool = True,
    largest_exchange_in_len: int = 0,
    smallest_nonempty_response_len: int | None = None,
) -> list[ConfusionSignal]:
    """Pure heuristic scorer — mirrors .claude/lib/rumil_skills/find_confusion.

    Does not read the DB: callers pass everything in. Kept pure so tests
    can drive it directly.
    """
    signals: list[ConfusionSignal] = []

    if call.status not in (CallStatus.COMPLETE,):
        signals.append(
            ConfusionSignal(
                name="non_complete_status",
                weight=_SIGNAL_WEIGHT["non_complete_status"],
                detail=f"status={call.status.value}",
            )
        )

    error_events = [e for e in (trace_events or []) if _event_name(e) == "error"]
    if error_events:
        first = error_events[0]
        msg = str(_event_field(first, "message") or "")[:60]
        signals.append(
            ConfusionSignal(
                name="trace_error",
                weight=_SIGNAL_WEIGHT["trace_error"],
                detail=f"{len(error_events)}x: {msg}",
            )
        )

    warning_events = [e for e in (trace_events or []) if _event_name(e) == "warning"]
    if len(warning_events) >= 2:
        signals.append(
            ConfusionSignal(
                name="multiple_warnings",
                weight=_SIGNAL_WEIGHT["multiple_warnings"],
                detail=f"{len(warning_events)} warnings",
            )
        )

    if exchange_errors:
        signals.append(
            ConfusionSignal(
                name="exchange_error",
                weight=_SIGNAL_WEIGHT["exchange_error"],
                detail=f"{exchange_errors} failed exchange(s)",
            )
        )

    if cost_usd is not None and cost_median and cost_usd > cost_median * COST_OUTLIER_MULTIPLIER:
        signals.append(
            ConfusionSignal(
                name="cost_outlier",
                weight=_SIGNAL_WEIGHT["cost_outlier"],
                detail=f"${cost_usd:.3f} vs ${cost_median:.3f} median",
            )
        )

    if (
        largest_exchange_in_len > HEURISTIC_INPUT_LARGE_THRESHOLD
        and smallest_nonempty_response_len is not None
        and smallest_nonempty_response_len < HEURISTIC_RESPONSE_SHORT_THRESHOLD
    ):
        signals.append(
            ConfusionSignal(
                name="thin_output",
                weight=_SIGNAL_WEIGHT["thin_output"],
                detail=(f"in={largest_exchange_in_len} out={smallest_nonempty_response_len}"),
            )
        )

    if not has_any_fruit:
        signals.append(
            ConfusionSignal(
                name="no_fruit",
                weight=_SIGNAL_WEIGHT["no_fruit"],
                detail="remaining_fruit unset or no created pages",
            )
        )

    return signals


def compute_confusion_score(signals: list[ConfusionSignal]) -> float:
    """Collapse signals into a [-1, 1] score.

    Positive = no signs of confusion. Each fired signal subtracts its
    weight, clamped to the [-1, 1] range. Clean calls return 1.0.
    """
    penalty = sum(s.weight for s in signals)
    return max(-1.0, min(1.0, 1.0 - 2.0 * penalty))


async def _load_trace_events(db: DB, call_id: str) -> list[dict]:
    """Read the persisted trace_json for one call.

    ``CallTrace`` persists events as it goes but does not hold them in
    memory after the call ends; the confusion scan reads them back from
    the ``calls.trace_json`` column.
    """
    try:
        q = db.client.table("calls").select("trace_json").eq("id", call_id).limit(1)
        rows = await db._execute(q)
    except Exception:
        log.debug("confusion_scan: trace_json lookup failed", exc_info=True)
        return []
    data = list(getattr(rows, "data", None) or [])
    if not data:
        return []
    trace = data[0].get("trace_json") or []
    if isinstance(trace, list):
        return trace
    return []


async def _rolling_cost_median(db: DB, call: Call) -> float | None:
    """Median cost of recent completed calls of the same type in this project.

    Small query — ``limit=30`` — intentionally cheap. Returns None when
    there's no baseline yet, which suppresses the cost_outlier signal.
    """
    try:
        q = (
            db.client.table("calls")
            .select("cost_usd")
            .eq("call_type", call.call_type.value)
            .eq("status", CallStatus.COMPLETE.value)
            .order("created_at", desc=True)
            .limit(30)
        )
        if db.project_id:
            q = q.eq("project_id", db.project_id)
        rows = await db._execute(q)
        data = list(getattr(rows, "data", None) or [])
    except Exception:
        log.debug("confusion_scan: cost median lookup failed", exc_info=True)
        return None
    costs = [r["cost_usd"] for r in data if r.get("cost_usd") is not None]
    if not costs:
        return None
    costs.sort()
    n = len(costs)
    mid = n // 2
    if n % 2 == 0:
        return (costs[mid - 1] + costs[mid]) / 2
    return costs[mid]


async def _count_exchange_errors(db: DB, call_id: str) -> int:
    try:
        q = (
            db.client.table("call_llm_exchanges")
            .select("error")
            .eq("call_id", call_id)
            .not_.is_("error", "null")
        )
        rows = await db._execute(q)
        data = list(getattr(rows, "data", None) or [])
    except Exception:
        log.debug("confusion_scan: exchange error lookup failed", exc_info=True)
        return 0
    return len(data)


async def _exchange_size_stats(db: DB, call_id: str) -> tuple[int, int | None]:
    """Return (largest_input_len, smallest_nonempty_response_len_for_that_input).

    Implements the same logic as the find-confusion skill's thin_output
    check: find an exchange with a big user_message and a tiny response.
    """
    try:
        q = (
            db.client.table("call_llm_exchanges")
            .select("user_message,response_text,tool_calls")
            .eq("call_id", call_id)
        )
        rows = await db._execute(q)
        data = list(getattr(rows, "data", None) or [])
    except Exception:
        log.debug("confusion_scan: exchange size lookup failed", exc_info=True)
        return 0, None
    worst_in = 0
    worst_out: int | None = None
    for ex in data:
        in_len = len(ex.get("user_message") or "")
        out_len = len(ex.get("response_text") or "")
        if ex.get("tool_calls"):
            continue
        if in_len > HEURISTIC_INPUT_LARGE_THRESHOLD and (worst_out is None or out_len < worst_out):
            worst_in = in_len
            worst_out = out_len
    return worst_in, worst_out


async def emit_confusion_scan_for_call(
    db: DB,
    call: Call,
    trace: CallTrace | None,
) -> None:
    """Compute confusion signals and emit one reputation event.

    Called by ``CallRunner._run_stages`` after ``mark_call_completed`` for
    every call. Failures are swallowed (the scan must never break the call
    pipeline). Writes always carry ``extra.subject_call_id`` so the
    expression index on ``reputation_events`` can serve later lookups.

    ``trace`` is accepted for future extension but unused — ``CallTrace``
    does not retain events in memory, so the confusion scan re-reads
    them from the persisted ``calls.trace_json``. This also means the
    scan sees exactly what downstream consumers see.
    """
    del trace
    try:
        trace_events = await _load_trace_events(db, call.id)
        cost_median = await _rolling_cost_median(db, call)
        exchange_errors = await _count_exchange_errors(db, call.id)
        worst_in, worst_out = await _exchange_size_stats(db, call.id)

        remaining_fruit = None
        review = call.review_json or {}
        if isinstance(review, dict):
            remaining_fruit = review.get("remaining_fruit")
        has_any_fruit = bool(remaining_fruit and remaining_fruit > 0)

        signals = compute_confusion_signals(
            call=call,
            trace_events=trace_events,
            cost_usd=call.cost_usd,
            cost_median=cost_median,
            exchange_errors=exchange_errors,
            has_any_fruit=has_any_fruit,
            largest_exchange_in_len=worst_in,
            smallest_nonempty_response_len=worst_out,
        )
        score = compute_confusion_score(signals)
        run_row = await db.get_run(db.run_id) if db.run_id else None
        config = (run_row or {}).get("config") or {} if isinstance(run_row, dict) else {}
        orchestrator = config.get("orchestrator") if isinstance(config, dict) else None
        await db.record_reputation_event(
            source="confusion_scan",
            dimension="confusion",
            score=score,
            orchestrator=orchestrator,
            source_call_id=call.id,
            extra={
                "subject_call_id": call.id,
                "subject_run_id": db.run_id,
                "signals": [
                    {"name": s.name, "weight": s.weight, "detail": s.detail} for s in signals
                ],
                "call_type": call.call_type.value,
            },
        )
    except Exception:
        log.exception("confusion_scan failed for call %s", call.id[:8])
