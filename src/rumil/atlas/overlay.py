"""Live-trace overlay: project a single run onto its workflow's stage diagram.

Takes a workflow profile + a ``run_id``, walks the run's calls and trace
events, and produces a per-stage annotation:

- ``fired``: the stage produced at least one event consistent with it
- ``skipped``: a ``PhaseSkippedEvent`` matched the stage
- ``iterations``: for loop stages, how many times the body re-entered
- ``calls``: which calls in the run map to this stage
- ``cost_usd`` / ``pages_loaded``: rolled up across mapped calls

The mapping uses the same ``call_params.phase`` heuristic as
``aggregate.py`` and additionally treats:
- ``execute_dispatches`` stages as collecting all dispatched children of
  the corresponding prioritization call
- ``view_refresh`` stages as collecting CREATE_VIEW / UPDATE_VIEW calls
- ``red_team`` stages as collecting RED_TEAM calls
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from typing import Any

from rumil.atlas import event_keys
from rumil.atlas.aggregate import (
    _calls_for_run,
    _count_dispatches,
    _count_pages_loaded,
    _duration_seconds,
    _events_of,
    _stages_for_call,
)
from rumil.atlas.schemas import (
    OverlayCall,
    WorkflowOverlay,
    WorkflowOverlayStage,
)
from rumil.atlas.workflows import get_workflow_profile
from rumil.database import DB
from rumil.models import CallType

_EXECUTE_BY_WORKFLOW: dict[str, dict[str, str]] = {
    "two_phase": {
        "initial_prioritization": "execute_dispatches",
        "main_phase_prioritization": "execute_dispatches",
    },
    "experimental": {
        "experimental_prioritization": "experimental_execute",
    },
    "claim_investigation": {
        "claim_phase1": "claim_execute",
        "claim_phase2_prioritization": "claim_execute",
    },
}

_VIEW_REFRESH_STAGE_BY_WORKFLOW: dict[str, str] = {
    "two_phase": "view_refresh",
    "experimental": "experimental_view_refresh",
}

_RED_TEAM_STAGE_BY_WORKFLOW: dict[str, str] = {
    "two_phase": "red_team",
}


def _children_of(parent_id: str, call_rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return [c for c in call_rows if c.get("parent_call_id") == parent_id]


def _to_overlay_call(c: dict[str, Any]) -> OverlayCall:
    events = _events_of(c)
    n_dispatches, _ = _count_dispatches(events)
    return OverlayCall(
        call_id=str(c.get("id") or ""),
        call_type=str(c.get("call_type") or ""),
        status=str(c.get("status") or ""),
        cost_usd=float(c.get("cost_usd") or 0.0),
        pages_loaded=_count_pages_loaded(events),
        n_dispatches=n_dispatches,
        started_at=c.get("created_at"),
        duration_seconds=_duration_seconds(c),
    )


def _max_duration(call_rows: Iterable[dict[str, Any]]) -> float | None:
    durations = [_duration_seconds(c) for c in call_rows]
    durations = [d for d in durations if d is not None]
    return max(durations) if durations else None


async def build_workflow_overlay(
    db: DB,
    workflow_name: str,
    run_id: str,
) -> WorkflowOverlay | None:
    profile = get_workflow_profile(workflow_name)
    if profile is None:
        return None

    call_rows = await _calls_for_run(db, run_id)
    if not call_rows:
        return WorkflowOverlay(
            workflow_name=workflow_name,
            run_id=run_id,
            profile=profile,
            stages=[WorkflowOverlayStage(stage_id=s.id, label=s.label) for s in profile.stages],
        )

    stage_calls: dict[str, list[dict[str, Any]]] = defaultdict(list)
    skipped_reasons: dict[str, str] = {}

    execute_map = _EXECUTE_BY_WORKFLOW.get(workflow_name, {})
    view_stage = _VIEW_REFRESH_STAGE_BY_WORKFLOW.get(workflow_name)
    red_stage = _RED_TEAM_STAGE_BY_WORKFLOW.get(workflow_name)

    prio_call_ids_by_phase_stage: dict[str, list[str]] = defaultdict(list)

    for c in call_rows:
        stage_id, skipped = _stages_for_call(workflow_name, c)
        if stage_id is None:
            continue
        if skipped:
            for e in _events_of(c):
                if e.get("event") == event_keys.PHASE_SKIPPED:
                    reason = e.get("reason")
                    if isinstance(reason, str):
                        skipped_reasons.setdefault(stage_id, reason)
                    break
            stage_calls.setdefault(stage_id, [])
        else:
            stage_calls[stage_id].append(c)
            if c.get("call_type") == CallType.PRIORITIZATION.value:
                prio_call_ids_by_phase_stage[stage_id].append(str(c.get("id")))

    for prio_stage_id, prio_ids in prio_call_ids_by_phase_stage.items():
        exec_stage_id = execute_map.get(prio_stage_id)
        if not exec_stage_id:
            continue
        for prio_id in prio_ids:
            for child in _children_of(prio_id, call_rows):
                ct = child.get("call_type")
                if ct == CallType.PRIORITIZATION.value:
                    continue
                if view_stage and ct in {
                    CallType.CREATE_VIEW.value,
                    CallType.UPDATE_VIEW.value,
                    CallType.CREATE_VIEW_MAX_EFFORT.value,
                    CallType.UPDATE_VIEW_MAX_EFFORT.value,
                    CallType.CREATE_FREEFORM_VIEW.value,
                    CallType.UPDATE_FREEFORM_VIEW.value,
                }:
                    stage_calls[view_stage].append(child)
                    continue
                if red_stage and ct == CallType.RED_TEAM.value:
                    stage_calls[red_stage].append(child)
                    continue
                stage_calls[exec_stage_id].append(child)

    stages: list[WorkflowOverlayStage] = []
    for stage in profile.stages:
        calls = stage_calls.get(stage.id, [])
        skipped_reason = skipped_reasons.get(stage.id)
        skipped = stage.id in skipped_reasons
        cost = sum(float(c.get("cost_usd") or 0.0) for c in calls)
        pages = sum(_count_pages_loaded(_events_of(c)) for c in calls)
        iterations = (
            len(calls) if stage.loop is False and stage.id.endswith("_loop") is False else 0
        )
        if stage.id in {
            "main_phase_prioritization",
            "experimental_prioritization",
            "claim_phase2_prioritization",
        }:
            iterations = sum(
                1 for c in calls if c.get("call_type") == CallType.PRIORITIZATION.value
            )
        stages.append(
            WorkflowOverlayStage(
                stage_id=stage.id,
                label=stage.label,
                fired=bool(calls) and not skipped,
                skipped=skipped,
                skipped_reason=skipped_reason,
                iterations=iterations,
                calls=[_to_overlay_call(c) for c in calls],
                cost_usd=round(cost, 4),
                pages_loaded=pages,
            )
        )

    started_at = min(
        (str(c.get("created_at") or "") for c in call_rows if c.get("created_at")),
        default=None,
    )
    finished_at = max(
        (str(c.get("completed_at") or "") for c in call_rows if c.get("completed_at")),
        default=None,
    )
    total_cost = sum(float(c.get("cost_usd") or 0.0) for c in call_rows)
    return WorkflowOverlay(
        workflow_name=workflow_name,
        run_id=run_id,
        profile=profile,
        stages=stages,
        n_calls=len(call_rows),
        cost_usd=round(total_cost, 4),
        duration_seconds=_max_duration(call_rows),
        started_at=started_at,
        finished_at=finished_at,
    )
