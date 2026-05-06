"""Prompt-edit impact: revision boundaries overlaid on call-type stats.

Closes the loop between ``/registry/prompts/{name}/history`` (when did
a prompt change?) and ``/calls/{ct}/stats`` (what's that call type
costing now?). For each adjacent pair of git revisions on a prompt
file, slice the call type's recent invocations into the [prev_ts,
this_ts) window and compute a tiny stats summary — mean cost, mean
rounds, mean pages, error/lying-complete pcts. Side-by-side rows
expose whether a prompt edit landed alongside a measurable shift.

The "prompt → call types affected" join is best-effort: atlas's
composition spec maps call types to prompt files, but bypass paths
exist (see drift agent #3 findings). Use this endpoint as a starting
point for investigation, not a closed-loop attribution.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from rumil.atlas import event_keys
from rumil.atlas.history import build_prompt_history
from rumil.atlas.schemas import PromptImpact, PromptImpactRevisionStats
from rumil.atlas.stats import (
    _calls_in_runs,
    _error_excerpt,
    _events,
    _mean,
    _pages_loaded,
    _recent_run_ids,
    _rounds_for_call,
)
from rumil.database import DB
from rumil.models import CallType


def _slice_window(
    rows: Sequence[dict[str, Any]],
    window_start: str | None,
    window_end: str | None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in rows:
        ts = r.get("created_at") or ""
        if window_start is not None and ts < window_start:
            continue
        if window_end is not None and ts >= window_end:
            continue
        out.append(r)
    return out


async def build_prompt_impact(
    db: DB,
    name: str,
    call_type: CallType,
    *,
    project_id: str | None = None,
    n_runs: int = 200,
    max_revisions: int = 6,
) -> PromptImpact | None:
    history = build_prompt_history(name, max_entries=max_revisions + 1)
    if history is None:
        return None
    entries = list(history.entries)
    if not entries:
        return PromptImpact(name=name, call_type=call_type.value, revisions=[], n_revisions=0)

    run_ids = await _recent_run_ids(db, project_id, n_runs)
    rows = await _calls_in_runs(db, run_ids, call_type=call_type.value)

    # Entries are newest-first. For each entry, the window is [next_ts, this_ts).
    revisions: list[PromptImpactRevisionStats] = []
    for i, e in enumerate(entries):
        # Skip the oldest entry — no prior revision to bound below
        # (could include "all earlier than this" but that conflates ages).
        prev_ts = entries[i + 1].commit_ts if i + 1 < len(entries) else None
        window_start = prev_ts
        window_end = e.commit_ts

        sliced = _slice_window(rows, window_start, window_end)
        n = len(sliced)
        if n == 0:
            revisions.append(
                PromptImpactRevisionStats(
                    commit_short=e.commit_short,
                    commit_ts=e.commit_ts,
                    subject=e.subject,
                    content_hash=e.content_hash,
                    window_start=window_start,
                    window_end=window_end,
                )
            )
            continue
        costs = [float(r.get("cost_usd") or 0.0) for r in sliced]
        rounds_list = [_rounds_for_call(_events(r)) for r in sliced]
        pages = [_pages_loaded(_events(r)) for r in sliced]
        n_with_error = 0
        n_lying_complete = 0
        for r in sliced:
            events = _events(r)
            had_error = any(ev.get("event") == event_keys.ERROR for ev in events) or bool(
                _error_excerpt(events)
            )
            if had_error:
                n_with_error += 1
                if str(r.get("status") or "").lower() == "complete":
                    n_lying_complete += 1
        revisions.append(
            PromptImpactRevisionStats(
                commit_short=e.commit_short,
                commit_ts=e.commit_ts,
                subject=e.subject,
                content_hash=e.content_hash,
                window_start=window_start,
                window_end=window_end,
                n_invocations=n,
                mean_cost_usd=_mean(costs),
                mean_rounds=_mean([float(x) for x in rounds_list]),
                mean_pages_loaded=_mean([float(x) for x in pages]),
                error_pct=round(100.0 * n_with_error / n, 2) if n else 0.0,
                lying_complete_pct=round(100.0 * n_lying_complete / n, 2) if n else 0.0,
            )
        )

    return PromptImpact(
        name=name,
        call_type=call_type.value,
        revisions=revisions,
        n_revisions=len(revisions),
    )
