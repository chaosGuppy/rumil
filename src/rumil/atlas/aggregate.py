"""Aggregate-behavior rollups across runs that used a given workflow.

Pulls runs whose ``config`` matches the workflow (via
``prioritizer_variant`` for orchestrators today; the same projection
extends naturally to versus workflows once they tag config). For each
run, walks the calls table + their ``trace_json`` to derive:

- which workflow stages fired (``DispatchesPlannedEvent``,
  ``ContextBuiltEvent``) vs were skipped (``PhaseSkippedEvent``)
- pages-loaded counts (``LoadPageEvent`` + ``ContextBuiltEvent``
  page tiers)
- dispatch-type frequencies (``DispatchExecutedEvent``)
- cost / duration aggregates (Call.cost_usd, completed_at - created_at)

Output schemas live in ``atlas.schemas`` so the FE can render
sparklines, histograms, branch-taken bars, and per-run drilldowns
without knowing the trace-event internals.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

from rumil.atlas.schemas import (
    DispatchFrequency,
    RunFlow,
    RunFlowNode,
    RunRollup,
    StageInvocation,
    WorkflowAggregate,
)
from rumil.atlas.workflows import get_workflow_profile
from rumil.database import DB
from rumil.models import CallType

log = logging.getLogger(__name__)


_WORKFLOW_TO_PRIORITIZER_VARIANT: dict[str, str] = {
    "two_phase": "two_phase",
    "experimental": "experimental",
}


def _run_matches_workflow(workflow_name: str, config: dict[str, Any]) -> bool:
    """Decide whether a row in ``runs`` belongs to ``workflow_name``.

    Handles both shapes the codebase uses today:

    - Orchestrator runs (``main.py``, ``scripts/run_call.py``) capture
      ``config.prioritizer_variant`` from settings; matched against the
      workflow's variant in ``_WORKFLOW_TO_PRIORITIZER_VARIANT``.
    - Versus runs (``versus/rumil_completion.py``,
      ``versus/rumil_judge.py``) tag ``config.origin == "versus"`` and
      either ``config.workflow == workflow_name`` (completion) or a
      ``config.task_name`` carrying the judge variant.
    """
    variant = _WORKFLOW_TO_PRIORITIZER_VARIANT.get(workflow_name)
    if variant is not None and config.get("prioritizer_variant") == variant:
        return True
    if config.get("origin") == "versus":
        if config.get("workflow") == workflow_name:
            return True
        if workflow_name == "reflective_judge" and config.get("task_name") == "reflective":
            return True
        if workflow_name == "two_phase_versus" and config.get("workflow") == "two_phase":
            return True
    return False


_STAGE_BY_CALL_TYPE_AND_PHASE: dict[str, dict[str, str]] = {
    "two_phase": {
        "prioritization:initial": "initial_prioritization",
        "prioritization:main_phase": "main_phase_prioritization",
    },
    "experimental": {
        "prioritization:main_phase": "experimental_prioritization",
    },
    "claim_investigation": {
        "prioritization:initial": "claim_phase1",
        "prioritization:main_phase": "claim_phase2_prioritization",
    },
}


async def _runs_for_workflow(
    db: DB,
    workflow_name: str,
    project_id: str | None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Return runs (newest first) configured for the given workflow.

    See ``_run_matches_workflow`` for the matching rules — covers both
    orchestrator runs (via ``config.prioritizer_variant``) and versus
    runs (via ``config.origin`` + ``config.workflow`` /
    ``config.task_name``).
    """
    query = (
        db.client.table("runs")
        .select("id, name, question_id, config, created_at, staged")
        .order("created_at", desc=True)
        .limit(max(limit * 6, 200))
    )
    if project_id:
        query = query.eq("project_id", project_id)
    res = await db._execute(query)
    rows: list[dict[str, Any]] = []
    for r in res.data or []:
        cfg = r.get("config") or {}
        if _run_matches_workflow(workflow_name, cfg):
            rows.append(r)
        if len(rows) >= limit:
            break
    return rows


async def _calls_for_run(db: DB, run_id: str) -> list[dict[str, Any]]:
    res = await db._execute(
        db.client.table("calls")
        .select(
            "id, call_type, parent_call_id, status, cost_usd, created_at, "
            "completed_at, scope_page_id, call_params, sequence_id, "
            "sequence_position, trace_json"
        )
        .eq("run_id", run_id)
        .order("created_at")
    )
    return list(res.data or [])


def _events_of(call_row: dict[str, Any]) -> list[dict[str, Any]]:
    raw = call_row.get("trace_json") or []
    if isinstance(raw, list):
        return [e for e in raw if isinstance(e, dict)]
    return []


def _count_pages_loaded(events: Iterable[dict[str, Any]]) -> int:
    """Sum unique page IDs surfaced by load_page + context_built tiers."""
    seen: set[str] = set()
    for e in events:
        et = e.get("event")
        if et == "load_page":
            pid = e.get("page_id")
            if isinstance(pid, str):
                seen.add(pid)
        elif et == "context_built":
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


def _count_dispatches(events: Iterable[dict[str, Any]]) -> tuple[int, dict[str, int]]:
    by_type: Counter[str] = Counter()
    for e in events:
        if e.get("event") == "dispatch_executed":
            ct = e.get("child_call_type")
            if isinstance(ct, str):
                by_type[ct] += 1
    return sum(by_type.values()), dict(by_type)


def _stages_for_call(workflow_name: str, call_row: dict[str, Any]) -> tuple[str | None, bool]:
    """Return (stage_id, was_skipped) for a single call.

    For prioritization calls we read ``call_params.phase`` to tell
    initial from main_phase. Skipped stages are detected via
    ``PhaseSkippedEvent``.
    """
    call_type = call_row.get("call_type")
    if call_type != CallType.PRIORITIZATION.value:
        return None, False
    params = call_row.get("call_params") or {}
    phase = params.get("phase") or "main_phase"
    key = f"prioritization:{phase}"
    stage = _STAGE_BY_CALL_TYPE_AND_PHASE.get(workflow_name, {}).get(key)
    skipped = any(e.get("event") == "phase_skipped" for e in _events_of(call_row))
    return stage, skipped


def _duration_seconds(call_row: dict[str, Any]) -> float | None:
    started = call_row.get("created_at")
    completed = call_row.get("completed_at")
    if not started or not completed:
        return None
    try:
        a = datetime.fromisoformat(started.replace("Z", "+00:00"))
        b = datetime.fromisoformat(completed.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return max((b - a).total_seconds(), 0.0)


_VIEW_REFRESH_BY_WORKFLOW = {
    "two_phase": "view_refresh",
    "experimental": "experimental_view_refresh",
}
_RED_TEAM_BY_WORKFLOW = {
    "two_phase": "red_team",
}
_EXECUTE_BY_WORKFLOW: dict[str, str] = {
    "two_phase": "execute_dispatches",
    "experimental": "experimental_execute",
    "claim_investigation": "claim_execute",
}
_VIEW_CALL_TYPES = {
    CallType.CREATE_VIEW.value,
    CallType.UPDATE_VIEW.value,
    CallType.CREATE_VIEW_MAX_EFFORT.value,
    CallType.UPDATE_VIEW_MAX_EFFORT.value,
    CallType.CREATE_FREEFORM_VIEW.value,
    CallType.UPDATE_FREEFORM_VIEW.value,
}


def _rollup_run(
    workflow_name: str,
    run_row: dict[str, Any],
    call_rows: Sequence[dict[str, Any]],
    question_headline: str | None,
) -> RunRollup:
    cost = 0.0
    n_dispatches = 0
    n_pages_loaded = 0
    dispatch_counts: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()
    stages_taken: set[str] = set()
    stages_skipped: set[str] = set()

    saw_dispatch_executed = False
    saw_view_call = False
    saw_red_team_call = False

    for c in call_rows:
        cost += float(c.get("cost_usd") or 0.0)
        status_counts[str(c.get("status") or "unknown")] += 1
        events = _events_of(c)
        n_pages_loaded += _count_pages_loaded(events)
        nd, by_type = _count_dispatches(events)
        n_dispatches += nd
        if nd:
            saw_dispatch_executed = True
        for k, v in by_type.items():
            dispatch_counts[k] += v
        stage, skipped = _stages_for_call(workflow_name, c)
        if stage:
            if skipped:
                stages_skipped.add(stage)
            else:
                stages_taken.add(stage)
        ct = c.get("call_type")
        if ct in _VIEW_CALL_TYPES:
            saw_view_call = True
        if ct == CallType.RED_TEAM.value:
            saw_red_team_call = True

    exec_stage = _EXECUTE_BY_WORKFLOW.get(workflow_name)
    if exec_stage and saw_dispatch_executed:
        stages_taken.add(exec_stage)
    view_stage = _VIEW_REFRESH_BY_WORKFLOW.get(workflow_name)
    if view_stage and saw_view_call:
        stages_taken.add(view_stage)
    red_stage = _RED_TEAM_BY_WORKFLOW.get(workflow_name)
    if red_stage and saw_red_team_call:
        stages_taken.add(red_stage)
    # Loop stages: marked as fired when any of their body stages fired.
    loop_pairs = {
        "two_phase": ("main_phase_loop", {"main_phase_prioritization", "execute_dispatches"}),
        "experimental": (
            "experimental_prio_loop",
            {"experimental_prioritization", "experimental_execute"},
        ),
        "claim_investigation": (
            "claim_main_loop",
            {"claim_phase2_prioritization", "claim_execute"},
        ),
        "draft_and_edit": (
            "dae_round_loop",
            {"dae_draft", "dae_critique"},
        ),
    }
    pair = loop_pairs.get(workflow_name)
    if pair is not None:
        loop_id, body = pair
        if any(b in stages_taken for b in body):
            stages_taken.add(loop_id)

    durations = [_duration_seconds(c) for c in call_rows]
    durations_clean = [d for d in durations if d is not None]
    duration = max(durations_clean) if durations_clean else None

    last_status: str | None = None
    if call_rows:
        last_status = (
            str(
                sorted(
                    call_rows,
                    key=lambda c: c.get("created_at") or "",
                )[-1].get("status")
                or ""
            )
            or None
        )

    return RunRollup(
        run_id=str(run_row.get("id") or ""),
        created_at=str(run_row.get("created_at") or ""),
        name=str(run_row.get("name") or ""),
        question_id=run_row.get("question_id"),
        question_headline=question_headline,
        n_calls=len(call_rows),
        n_dispatches=n_dispatches,
        n_pages_loaded=n_pages_loaded,
        cost_usd=round(cost, 4),
        duration_seconds=duration,
        last_status=last_status,
        stages_taken=sorted(stages_taken),
        stages_skipped=sorted(stages_skipped),
        dispatch_counts=dict(dispatch_counts),
        call_status_counts=dict(status_counts),
    )


async def build_workflow_aggregate(
    db: DB,
    workflow_name: str,
    project_id: str | None = None,
    limit: int = 50,
) -> WorkflowAggregate:
    """Compute the aggregate rollup for one workflow over recent runs."""
    profile = get_workflow_profile(workflow_name)
    if profile is None:
        return WorkflowAggregate(
            workflow_name=workflow_name,
            n_runs=0,
            runs=[],
            stage_invocations=[],
            dispatch_frequencies=[],
        )

    run_rows = await _runs_for_workflow(db, workflow_name, project_id, limit=limit)
    rollups: list[RunRollup] = []

    question_ids = [r.get("question_id") for r in run_rows if r.get("question_id")]
    pages_by_id = (
        await db.get_pages_by_ids(list({q for q in question_ids if q})) if question_ids else {}
    )

    for run in run_rows:
        rid = run.get("id")
        if not rid:
            continue
        call_rows = await _calls_for_run(db, str(rid))
        qid = run.get("question_id")
        page = pages_by_id.get(qid) if qid else None
        headline = page.headline if page is not None else None
        rollups.append(_rollup_run(workflow_name, run, call_rows, headline))

    stage_invocations = _stage_invocations(profile, rollups)
    dispatch_freq = _dispatch_frequencies(rollups)

    return WorkflowAggregate(
        workflow_name=workflow_name,
        n_runs=len(rollups),
        runs=rollups,
        stage_invocations=stage_invocations,
        dispatch_frequencies=dispatch_freq,
        pages_loaded_per_run=[r.n_pages_loaded for r in rollups],
        cost_per_run=[r.cost_usd for r in rollups],
        dispatches_per_run=[r.n_dispatches for r in rollups],
        calls_per_run=[r.n_calls for r in rollups],
        sparkline=[float(r.n_dispatches) for r in rollups],
    )


def _stage_invocations(profile, rollups: Sequence[RunRollup]) -> list[StageInvocation]:
    out: list[StageInvocation] = []
    for stage in profile.stages:
        taken = sum(1 for r in rollups if stage.id in r.stages_taken)
        skipped = sum(1 for r in rollups if stage.id in r.stages_skipped)
        out.append(
            StageInvocation(
                stage_id=stage.id,
                label=stage.label,
                taken_count=taken,
                skipped_count=skipped,
                total_runs=len(rollups),
            )
        )
    return out


def _dispatch_frequencies(rollups: Sequence[RunRollup]) -> list[DispatchFrequency]:
    n_runs = max(len(rollups), 1)
    totals: Counter[str] = Counter()
    runs_with: Counter[str] = Counter()
    for r in rollups:
        for ct, n in r.dispatch_counts.items():
            totals[ct] += n
            if n > 0:
                runs_with[ct] += 1
    out = [
        DispatchFrequency(
            call_type=ct,
            total=total,
            avg_per_run=round(total / n_runs, 3),
            runs_with_at_least_one=runs_with[ct],
        )
        for ct, total in totals.most_common()
    ]
    return out


async def build_run_flow(db: DB, run_id: str) -> RunFlow:
    """Per-run flow: each call as a node, with stage labels where derivable."""
    run_row = await db.get_run(run_id)
    workflow_name: str | None = None
    if run_row:
        cfg = run_row.get("config") or {}
        variant = cfg.get("prioritizer_variant")
        for name, v in _WORKFLOW_TO_PRIORITIZER_VARIANT.items():
            if v == variant:
                workflow_name = name
                break
    call_rows = await _calls_for_run(db, run_id)
    nodes: list[RunFlowNode] = []
    from rumil.atlas.descriptions import CALL_TYPE_DESCRIPTIONS

    for c in call_rows:
        events = _events_of(c)
        stage_id, _skipped = _stages_for_call(workflow_name, c) if workflow_name else (None, False)
        n_dispatches, _ = _count_dispatches(events)
        pages_loaded = _count_pages_loaded(events)
        try:
            ct_enum = CallType(c.get("call_type"))
            ct_desc = CALL_TYPE_DESCRIPTIONS.get(ct_enum, "")
        except ValueError:
            ct_desc = ""
        nodes.append(
            RunFlowNode(
                call_id=str(c.get("id") or ""),
                parent_call_id=c.get("parent_call_id"),
                call_type=str(c.get("call_type") or ""),
                call_type_description=ct_desc,
                status=str(c.get("status") or ""),
                cost_usd=float(c.get("cost_usd") or 0.0),
                pages_loaded=pages_loaded,
                n_dispatches=n_dispatches,
                started_at=c.get("created_at"),
                duration_seconds=_duration_seconds(c),
                stage_id=stage_id,
                summary="",
            )
        )
    return RunFlow(run_id=run_id, workflow_name=workflow_name, nodes=nodes)
