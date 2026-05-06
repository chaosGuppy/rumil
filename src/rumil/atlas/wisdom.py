"""Surfaces that show what rumil's actually *producing* — not just
how runs are shaped, but the claims, judgements, and views the
system has been emitting. Closes a gap atlas had: structure-only
visibility couldn't answer "is the system getting wiser?"

Two surfaces:

- ``build_recent_work_feed``: a chronological feed of recent pages
  (claims / judgements / views) with provenance — which run, which
  call_type, what workflow produced it. Optional filters by
  workflow / project / page_type.

- ``build_question_trajectory``: every judgement and view a question
  has accumulated across all runs, in time order, with credence /
  robustness deltas and the considerations that landed between
  judgement pairs. The "is this question converging" view.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from rumil.atlas.schemas import (
    QuestionTrajectory,
    RecentWorkFeed,
    RecentWorkItem,
    TrajectoryConsideration,
    TrajectoryJudgement,
    TrajectoryView,
)
from rumil.database import DB

log = logging.getLogger(__name__)


_WISDOM_PAGE_TYPES_DEFAULT = ("judgement", "claim", "view")


def _truncate(s: str | None, n: int) -> str:
    if not s:
        return ""
    if len(s) <= n:
        return s
    return s[: n - 1] + "…"


def _workflow_for_run_config(cfg: dict[str, Any] | None) -> str | None:
    if not isinstance(cfg, dict):
        return None
    if cfg.get("origin") == "versus":
        return cfg.get("workflow") or cfg.get("task_name")
    variant = cfg.get("prioritizer_variant")
    if variant in ("two_phase", "experimental"):
        return variant
    return None


async def build_recent_work_feed(
    db: DB,
    *,
    project_id: str | None = None,
    page_types: Sequence[str] = _WISDOM_PAGE_TYPES_DEFAULT,
    workflow_name: str | None = None,
    limit: int = 50,
) -> RecentWorkFeed:
    """Recent pages produced by the system, with workflow/run/call
    provenance. Ordered newest first.
    """
    query = (
        db.client.table("pages")
        .select(
            "id, page_type, headline, abstract, content, created_at, "
            "project_id, run_id, provenance_call_id, provenance_call_type, "
            "credence, credence_reasoning, robustness, "
            "importance, superseded_by"
        )
        .in_("page_type", list(page_types))
        .order("created_at", desc=True)
        .limit(max(limit * 4, 100) if workflow_name else limit)
    )
    if project_id:
        query = query.eq("project_id", project_id)
    res = await db._execute(query)
    rows = list(res.data or [])
    if not rows:
        return RecentWorkFeed(
            items=[],
            n_items=0,
            filters_applied={
                k: v
                for k, v in {
                    "project_id": project_id,
                    "workflow_name": workflow_name,
                    "page_types": ",".join(page_types),
                }.items()
                if v
            },
        )

    run_ids = list({str(r.get("run_id")) for r in rows if r.get("run_id")})
    project_ids = list({str(r.get("project_id")) for r in rows if r.get("project_id")})

    runs_by_id: dict[str, dict[str, Any]] = {}
    if run_ids:
        rres = await db._execute(
            db.client.table("runs").select("id, name, config").in_("id", run_ids)
        )
        for r in rres.data or []:
            runs_by_id[str(r.get("id") or "")] = r

    projects_by_id: dict[str, str] = {}
    if project_ids:
        pres = await db._execute(
            db.client.table("projects").select("id, name").in_("id", project_ids)
        )
        for p in pres.data or []:
            projects_by_id[str(p.get("id") or "")] = str(p.get("name") or "")

    items: list[RecentWorkItem] = []
    for r in rows:
        rid = str(r.get("run_id") or "")
        run_row = runs_by_id.get(rid) or {}
        wf = _workflow_for_run_config(run_row.get("config"))
        if workflow_name and wf != workflow_name:
            continue
        pid = str(r.get("project_id") or "")
        items.append(
            RecentWorkItem(
                page_id=str(r.get("id") or ""),
                page_type=str(r.get("page_type") or ""),
                headline=str(r.get("headline") or ""),
                abstract=str(r.get("abstract") or ""),
                content_preview=_truncate(r.get("content"), 480),
                created_at=str(r.get("created_at") or ""),
                project_id=pid,
                project_name=projects_by_id.get(pid),
                run_id=rid,
                run_name=str(run_row.get("name") or "") or None,
                workflow_name=wf,
                call_id=str(r.get("provenance_call_id") or ""),
                call_type=str(r.get("provenance_call_type") or ""),
                credence=r.get("credence"),
                credence_reasoning=str(r.get("credence_reasoning") or ""),
                robustness=r.get("robustness"),
                importance=r.get("importance"),
                superseded=bool(r.get("superseded_by")),
            )
        )
        if len(items) >= limit:
            break

    return RecentWorkFeed(
        items=items,
        n_items=len(items),
        filters_applied={
            k: v
            for k, v in {
                "project_id": project_id,
                "workflow_name": workflow_name,
                "page_types": ",".join(page_types),
            }.items()
            if v
        },
    )


async def _judgements_for_question(db: DB, question_id: str) -> list[dict[str, Any]]:
    """Judgement pages linked to ``question_id`` via an ANSWERS link."""
    lres = await db._execute(
        db.client.table("page_links")
        .select("from_page_id, to_page_id, link_type")
        .eq("to_page_id", question_id)
        .eq("link_type", "answers")
    )
    judgement_ids = list(
        {str(r.get("from_page_id")) for r in (lres.data or []) if r.get("from_page_id")}
    )
    if not judgement_ids:
        return []
    pres = await db._execute(
        db.client.table("pages")
        .select(
            "id, page_type, headline, abstract, content, created_at, "
            "run_id, provenance_call_id, provenance_call_type, "
            "credence, credence_reasoning, robustness, superseded_by"
        )
        .in_("id", judgement_ids)
    )
    rows = [r for r in (pres.data or []) if r.get("page_type") == "judgement"]
    rows.sort(key=lambda r: str(r.get("created_at") or ""))
    return rows


async def _views_for_question(db: DB, question_id: str) -> list[dict[str, Any]]:
    """View pages linked to ``question_id`` via a VIEW_OF link."""
    lres = await db._execute(
        db.client.table("page_links")
        .select("from_page_id")
        .eq("to_page_id", question_id)
        .eq("link_type", "view_of")
    )
    view_ids = list(
        {str(r.get("from_page_id")) for r in (lres.data or []) if r.get("from_page_id")}
    )
    if not view_ids:
        return []
    pres = await db._execute(
        db.client.table("pages")
        .select("id, page_type, headline, abstract, created_at, run_id, superseded_by")
        .in_("id", view_ids)
    )
    rows = [r for r in (pres.data or []) if r.get("page_type") == "view"]
    rows.sort(key=lambda r: str(r.get("created_at") or ""))
    return rows


async def _considerations_for_question(db: DB, question_id: str) -> list[dict[str, Any]]:
    """Consideration links into the question, with the originating
    claim or page joined."""
    lres = await db._execute(
        db.client.table("page_links")
        .select("from_page_id, link_type, direction, strength, role, created_at")
        .eq("to_page_id", question_id)
        .eq("link_type", "consideration")
        .order("created_at")
    )
    link_rows = list(lres.data or [])
    if not link_rows:
        return []
    page_ids = list({str(r.get("from_page_id")) for r in link_rows if r.get("from_page_id")})
    pres = await db._execute(
        db.client.table("pages")
        .select(
            "id, page_type, headline, abstract, created_at, run_id, "
            "provenance_call_type, credence, robustness"
        )
        .in_("id", page_ids)
    )
    page_by_id = {str(p.get("id") or ""): p for p in (pres.data or [])}

    out: list[dict[str, Any]] = []
    for lr in link_rows:
        pid = str(lr.get("from_page_id") or "")
        page = page_by_id.get(pid)
        if not page:
            continue
        merged = dict(page)
        merged["link_direction"] = lr.get("direction")
        merged["link_strength"] = lr.get("strength")
        merged["link_role"] = lr.get("role")
        merged["link_created_at"] = lr.get("created_at")
        out.append(merged)
    out.sort(key=lambda r: str(r.get("link_created_at") or r.get("created_at") or ""))
    return out


def _stdev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    m = sum(values) / len(values)
    sq = sum((v - m) ** 2 for v in values)
    return (sq / (len(values) - 1)) ** 0.5


async def build_question_trajectory(db: DB, question_id: str) -> QuestionTrajectory | None:
    """Full trajectory of a question's judgements, views, and the
    considerations that landed in each judgement window.
    """
    page = await db.get_page(question_id)
    if page is None or page.page_type.value != "question":
        return None

    judgement_rows = await _judgements_for_question(db, question_id)
    view_rows = await _views_for_question(db, question_id)
    consideration_rows = await _considerations_for_question(db, question_id)

    run_ids = list(
        {
            str(r.get("run_id"))
            for r in [*judgement_rows, *view_rows, *consideration_rows]
            if r.get("run_id")
        }
    )
    runs_by_id: dict[str, dict[str, Any]] = {}
    if run_ids:
        rres = await db._execute(db.client.table("runs").select("id, name").in_("id", run_ids))
        for r in rres.data or []:
            runs_by_id[str(r.get("id") or "")] = r

    judgements: list[TrajectoryJudgement] = []
    prev_credence: int | None = None
    prev_robustness: int | None = None
    credences: list[int] = []
    for j in judgement_rows:
        credence = j.get("credence")
        robustness = j.get("robustness")
        rid = str(j.get("run_id") or "")
        run_row = runs_by_id.get(rid) or {}
        delta_c = (
            (credence - prev_credence)
            if (isinstance(credence, int) and isinstance(prev_credence, int))
            else None
        )
        delta_r = (
            (robustness - prev_robustness)
            if (isinstance(robustness, int) and isinstance(prev_robustness, int))
            else None
        )
        judgements.append(
            TrajectoryJudgement(
                page_id=str(j.get("id") or ""),
                headline=str(j.get("headline") or ""),
                abstract=str(j.get("abstract") or ""),
                content=_truncate(j.get("content"), 1200),
                credence=credence,
                credence_reasoning=_truncate(j.get("credence_reasoning"), 480),
                robustness=robustness,
                created_at=str(j.get("created_at") or ""),
                run_id=rid,
                run_name=str(run_row.get("name") or "") or None,
                call_id=str(j.get("provenance_call_id") or ""),
                call_type=str(j.get("provenance_call_type") or ""),
                superseded_by=str(j.get("superseded_by") or "") or None,
                delta_credence=delta_c,
                delta_robustness=delta_r,
            )
        )
        if isinstance(credence, int):
            credences.append(credence)
            prev_credence = credence
        if isinstance(robustness, int):
            prev_robustness = robustness

    views: list[TrajectoryView] = []
    for v in view_rows:
        views.append(
            TrajectoryView(
                page_id=str(v.get("id") or ""),
                headline=str(v.get("headline") or ""),
                abstract=str(v.get("abstract") or ""),
                created_at=str(v.get("created_at") or ""),
                run_id=str(v.get("run_id") or ""),
                superseded_by=str(v.get("superseded_by") or "") or None,
            )
        )

    judgement_anchor_ts = [(j.created_at, j.page_id) for j in judgements]
    considerations: list[TrajectoryConsideration] = []
    for c in consideration_rows:
        landed_at = str(c.get("link_created_at") or c.get("created_at") or "")
        landed_after: str | None = None
        landed_before: str | None = None
        for ts, jid in judgement_anchor_ts:
            if landed_at >= ts:
                landed_after = jid
            else:
                landed_before = jid
                break
        considerations.append(
            TrajectoryConsideration(
                page_id=str(c.get("id") or ""),
                page_type=str(c.get("page_type") or ""),
                headline=str(c.get("headline") or ""),
                abstract=_truncate(c.get("abstract"), 240),
                credence=c.get("credence"),
                robustness=c.get("robustness"),
                direction=c.get("link_direction"),
                strength=c.get("link_strength"),
                role=c.get("link_role"),
                created_at=landed_at,
                run_id=str(c.get("run_id") or ""),
                call_type=str(c.get("provenance_call_type") or ""),
                landed_after_judgement_id=landed_after,
                landed_before_judgement_id=landed_before,
            )
        )

    n_runs_touched = len(
        {
            str(r.get("run_id"))
            for r in [*judgement_rows, *view_rows, *consideration_rows]
            if r.get("run_id")
        }
    )
    cred_floats = [float(c) for c in credences]
    volatility = round(_stdev(cred_floats), 3)
    latest_credence = credences[-1] if credences else None
    latest_robustness: int | None = None
    if judgements:
        latest_robustness = judgements[-1].robustness

    converging: bool | None = None
    if len(credences) >= 3:
        # Recent half should be tighter than the older half.
        half = len(credences) // 2
        recent = credences[-half:]
        older = credences[:half]
        rec_sd = _stdev([float(x) for x in recent])
        old_sd = _stdev([float(x) for x in older])
        converging = rec_sd <= max(old_sd * 0.7, 0.5)

    return QuestionTrajectory(
        question_id=question_id,
        question_headline=page.headline or "",
        question_abstract=page.abstract or "",
        project_id=str(getattr(page, "project_id", "") or ""),
        n_runs_touched=n_runs_touched,
        n_judgements=len(judgements),
        n_views=len(views),
        n_considerations=len(considerations),
        judgements=judgements,
        views=views,
        considerations=considerations,
        credences=credences,
        credence_volatility=volatility,
        latest_credence=latest_credence,
        latest_robustness=latest_robustness,
        converging=converging,
    )
