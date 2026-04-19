"""Shared constants and small helpers for the eval-feedback pipeline.

Central place for the dimensions we surface into prioritization, so
prompts / tests / policies don't drift. The dimensions here match
``EVAL_AGENTS`` in ``rumil.run_eval.agents`` plus ``confusion`` from the
cheap confusion_scan.
"""

from __future__ import annotations

from collections.abc import Sequence

from rumil.database import EvalSummary

PRIORITIZATION_EVAL_DIMENSIONS: Sequence[str] = (
    "grounding",
    "calibration",
    "research_progress",
    "consistency",
    "general_quality",
    "subquestion_relevance",
    "quality_control",
    "confusion",
)

LAZY_EVAL_DIMENSIONS: Sequence[str] = ("grounding", "calibration")


def format_eval_summary_line(
    summaries: dict[str, EvalSummary] | None,
    *,
    dimensions: Sequence[str] = PRIORITIZATION_EVAL_DIMENSIONS,
) -> str:
    """Render a compact inline ``grounding=4.2 (n=3), calibration=6.0 (n=2)``.

    Dimensions with zero events are omitted (keeps the line short).
    Returns the empty string when nothing fires.
    """
    if not summaries:
        return ""
    parts: list[str] = []
    for dim in dimensions:
        s = summaries.get(dim)
        if s is None or s.count == 0:
            continue
        parts.append(f"{dim}={s.mean:.2f} (n={s.count})")
    return ", ".join(parts)


def select_lazy_eval_targets(
    page_ids: Sequence[str],
    *,
    summaries: dict[str, dict[str, EvalSummary]],
    dimensions: Sequence[str] = LAZY_EVAL_DIMENSIONS,
    per_round_cap: int,
    already_evaluated_this_run: int,
    per_run_cap: int,
) -> list[str]:
    """Pick up to ``per_round_cap`` pages that lack any of ``dimensions``.

    Phase 3 of evals-as-feedback: at prioritization time, orchestrators
    call this to decide which claims/subquestions deserve a cheap lazy
    eval pass before scoring. Mandatory hedges enforced:

    - Respects per-run spend ceiling: the returned list shrinks so
      ``already_evaluated_this_run + len(result) <= per_run_cap``.
    - Skips pages that already have at least one event for EVERY
      requested dimension — those are "covered" for this round.
    - Preserves input order so the caller's importance-based ranking
      determines which uncovered pages win when the cap bites.

    Pure function — callers (two_phase / policies) combine this with
    ``settings.lazy_eval_enabled`` gating + actual dispatch.
    """
    if per_round_cap <= 0 or already_evaluated_this_run >= per_run_cap:
        return []
    remaining_budget = per_run_cap - already_evaluated_this_run
    cap = min(per_round_cap, remaining_budget)
    if cap <= 0:
        return []
    out: list[str] = []
    for pid in page_ids:
        covered = summaries.get(pid) or {}
        missing = any(covered.get(dim) is None or covered[dim].count == 0 for dim in dimensions)
        if missing:
            out.append(pid)
            if len(out) >= cap:
                break
    return out
