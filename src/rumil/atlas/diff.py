"""Run-vs-run diff: align two runs on a workflow's stage diagram and
report per-stage deltas.

When both runs map to the same workflow, alignment is straightforward
(stage_id matches stage_id). When they're different workflows the diff
falls back to a flat top-line comparison without stage rows. Useful
for "why did this run cost $6 and the other $1?" debugging.
"""

from __future__ import annotations

from rumil.atlas.overlay import build_workflow_overlay
from rumil.atlas.schemas import (
    DispatchCountDiff,
    RunDiff,
    RunDiffSide,
    StageDiffRow,
    WorkflowOverlay,
)
from rumil.atlas.workflows import get_workflow_profile
from rumil.database import DB


def _workflow_for_run(run_row: dict | None) -> str | None:
    if not run_row:
        return None
    cfg = run_row.get("config") or {}
    if cfg.get("origin") == "versus":
        return cfg.get("workflow") or cfg.get("task_name")
    variant = cfg.get("prioritizer_variant")
    if variant in ("two_phase", "experimental"):
        return variant
    return None


def _side_from_overlay(overlay: WorkflowOverlay | None, run_row: dict | None) -> RunDiffSide:
    name = (run_row or {}).get("name") or ""
    if overlay is None:
        return RunDiffSide(
            run_id=str((run_row or {}).get("id") or ""),
            name=str(name),
            workflow_name=None,
            cost_usd=0.0,
            n_calls=0,
            n_dispatches=0,
            pages_loaded=0,
        )
    pages = sum(s.pages_loaded for s in overlay.stages)
    n_dispatches = sum(c.n_dispatches for s in overlay.stages for c in s.calls)
    return RunDiffSide(
        run_id=overlay.run_id,
        name=str(name),
        workflow_name=overlay.workflow_name,
        cost_usd=overlay.cost_usd,
        n_calls=overlay.n_calls,
        n_dispatches=n_dispatches,
        pages_loaded=pages,
        duration_seconds=overlay.duration_seconds,
        started_at=overlay.started_at,
    )


async def build_run_diff(db: DB, run_a_id: str, run_b_id: str) -> RunDiff:
    run_a_row = await db.get_run(run_a_id)
    run_b_row = await db.get_run(run_b_id)
    workflow_a = _workflow_for_run(run_a_row)
    workflow_b = _workflow_for_run(run_b_row)
    same_workflow = workflow_a is not None and workflow_a == workflow_b

    overlay_a: WorkflowOverlay | None = None
    overlay_b: WorkflowOverlay | None = None
    if workflow_a and get_workflow_profile(workflow_a):
        overlay_a = await build_workflow_overlay(db, workflow_a, run_a_id)
    if workflow_b and get_workflow_profile(workflow_b):
        overlay_b = await build_workflow_overlay(db, workflow_b, run_b_id)

    side_a = _side_from_overlay(overlay_a, run_a_row)
    side_b = _side_from_overlay(overlay_b, run_b_row)

    notes: list[str] = []
    aligned = workflow_a if same_workflow else None
    stages: list[StageDiffRow] = []
    dispatch_diffs: list[DispatchCountDiff] = []

    if same_workflow and overlay_a is not None and overlay_b is not None:
        a_stages = {s.stage_id: s for s in overlay_a.stages}
        b_stages = {s.stage_id: s for s in overlay_b.stages}
        profile = get_workflow_profile(workflow_a or "")
        ordered_ids: list[str] = []
        if profile is not None:
            ordered_ids = [s.id for s in profile.stages]
        for sid in a_stages:
            if sid not in ordered_ids:
                ordered_ids.append(sid)
        for sid in b_stages:
            if sid not in ordered_ids:
                ordered_ids.append(sid)
        for sid in ordered_ids:
            sa = a_stages.get(sid)
            sb = b_stages.get(sid)
            either = sa or sb
            label = either.label if either is not None else sid
            stages.append(
                StageDiffRow(
                    stage_id=sid,
                    label=label,
                    a_fired=bool(sa and sa.fired),
                    b_fired=bool(sb and sb.fired),
                    a_skipped=bool(sa and sa.skipped),
                    b_skipped=bool(sb and sb.skipped),
                    a_iterations=sa.iterations if sa else 0,
                    b_iterations=sb.iterations if sb else 0,
                    a_cost_usd=sa.cost_usd if sa else 0.0,
                    b_cost_usd=sb.cost_usd if sb else 0.0,
                    a_pages_loaded=sa.pages_loaded if sa else 0,
                    b_pages_loaded=sb.pages_loaded if sb else 0,
                    a_n_calls=len(sa.calls) if sa else 0,
                    b_n_calls=len(sb.calls) if sb else 0,
                )
            )
        # n_dispatches already populated by _side_from_overlay; no-op here.

    if not same_workflow:
        if workflow_a is None:
            notes.append(f"Run {run_a_id[:8]}: no workflow detected from runs.config.")
        if workflow_b is None:
            notes.append(f"Run {run_b_id[:8]}: no workflow detected from runs.config.")
        if workflow_a and workflow_b and workflow_a != workflow_b:
            notes.append(
                f"Different workflows ({workflow_a!r} vs {workflow_b!r}); stage rows omitted."
            )

    if same_workflow and overlay_a is not None and overlay_b is not None:
        a_counts: dict[str, int] = {}
        b_counts: dict[str, int] = {}
        for s in overlay_a.stages:
            for c in s.calls:
                a_counts[c.call_type] = a_counts.get(c.call_type, 0) + 1
        for s in overlay_b.stages:
            for c in s.calls:
                b_counts[c.call_type] = b_counts.get(c.call_type, 0) + 1
        all_types = sorted(set(a_counts) | set(b_counts))
        for ct in all_types:
            ac = a_counts.get(ct, 0)
            bc = b_counts.get(ct, 0)
            if ac != bc:
                dispatch_diffs.append(DispatchCountDiff(call_type=ct, a_count=ac, b_count=bc))

    return RunDiff(
        a=side_a,
        b=side_b,
        same_workflow=same_workflow,
        aligned_workflow=aligned,
        stages=stages,
        dispatch_diffs=dispatch_diffs,
        notes=notes,
    )
