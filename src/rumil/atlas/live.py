"""Live snapshot of a run for the workflow-overlay UI.

Returns the same overlay shape as ``build_workflow_overlay`` plus a
small in-flight signal set: whether any call is still pending/running,
the most recent trace-event timestamp, and a best-guess "current stage"
based on which of the workflow's stages most recently received activity.

The streaming variant lives next door — this module is the polling
primitive. Frontends can poll this endpoint at ~2s intervals while a
run is live and stop when ``is_in_flight`` flips to False.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from rumil.atlas.aggregate import _calls_for_run, _events_of
from rumil.atlas.overlay import build_workflow_overlay
from rumil.atlas.schemas import LiveRunSnapshot, WorkflowOverlay
from rumil.atlas.workflows import get_workflow_profile
from rumil.database import DB


def _workflow_for_run(run_row: dict[str, Any] | None) -> str | None:
    if not run_row:
        return None
    cfg = run_row.get("config") or {}
    if cfg.get("origin") == "versus":
        return cfg.get("workflow") or cfg.get("task_name")
    variant = cfg.get("prioritizer_variant")
    if variant in ("two_phase", "experimental"):
        return variant
    return None


def _last_event_ts(call_rows: list[dict[str, Any]]) -> str | None:
    """Return the most recent timestamp visible across calls + their events.

    Falls back to call ``completed_at`` / ``created_at`` when trace events
    don't carry their own ``ts``. Strings compared lexicographically — ISO
    8601 is sortable as text when the suffixes match.
    """
    latest: str | None = None
    for c in call_rows:
        for ts_key in ("completed_at", "created_at"):
            v = c.get(ts_key)
            if isinstance(v, str) and (latest is None or v > latest):
                latest = v
        for e in _events_of(c):
            ts = e.get("ts")
            if isinstance(ts, str) and (latest is None or ts > latest):
                latest = ts
    return latest


def _current_stage_id(
    overlay: WorkflowOverlay | None, call_rows: list[dict[str, Any]]
) -> str | None:
    """Best-guess "we are currently in stage X" for highlighting.

    Picks the latest-activity stage that has at least one in-flight call
    (PENDING/RUNNING); falls back to the latest stage with any activity.
    """
    if overlay is None:
        return None

    in_flight_call_ids = {
        str(c.get("id")) for c in call_rows if c.get("status") in ("pending", "running")
    }

    best: tuple[str, str] | None = None
    for stage in overlay.stages:
        if not stage.calls:
            continue
        latest_ts: str | None = None
        has_in_flight = False
        for ref in stage.calls:
            if ref.call_id in in_flight_call_ids:
                has_in_flight = True
            ts = ref.started_at
            if isinstance(ts, str) and (latest_ts is None or ts > latest_ts):
                latest_ts = ts
        if not latest_ts:
            continue
        key = f"{1 if has_in_flight else 0}|{latest_ts}"
        if best is None or key > best[1]:
            best = (stage.stage_id, key)
    return best[0] if best else None


async def build_live_snapshot(db: DB, run_id: str) -> LiveRunSnapshot:
    run_row = await db.get_run(run_id)
    workflow_name = _workflow_for_run(run_row)

    call_rows = await _calls_for_run(db, run_id)
    n_pending = sum(1 for c in call_rows if c.get("status") == "pending")
    n_running = sum(1 for c in call_rows if c.get("status") == "running")
    is_in_flight = (n_pending + n_running) > 0

    overlay: WorkflowOverlay | None = None
    if workflow_name and get_workflow_profile(workflow_name):
        overlay = await build_workflow_overlay(db, workflow_name, run_id)

    return LiveRunSnapshot(
        run_id=run_id,
        workflow_name=workflow_name,
        is_in_flight=is_in_flight,
        last_event_ts=_last_event_ts(call_rows),
        current_stage_id=_current_stage_id(overlay, call_rows),
        overlay=overlay,
        n_pending_calls=n_pending,
        n_running_calls=n_running,
        snapshot_ts=datetime.now(UTC).isoformat(),
    )
