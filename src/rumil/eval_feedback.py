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
