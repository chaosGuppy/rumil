"""Calibration eval helpers.

The calibration eval agent walks the run's new claims, asks a reviewer model
"on a 1-9 scale, what credence do you assign to this claim given its cited
sources?", and compares the reviewer's verdict to the self-reported credence.

This module holds the pure computation pieces (dataclass + scoring function)
used by the agent prompt and the tests. The agent itself is a standard
``EvalAgentSpec`` driven by ``prompts/run-eval-calibration.md`` — it uses
``load_page`` to read claims+sources and ``explore_subgraph`` to find them.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

log = logging.getLogger(__name__)

MIN_CREDENCE = 1
MAX_CREDENCE = 9
CREDENCE_SPAN = MAX_CREDENCE - MIN_CREDENCE


@dataclass(frozen=True)
class CredenceComparison:
    """One sampled claim: self-report vs reviewer credence."""

    claim_id: str
    headline: str
    self_credence: int | None
    reviewer_credence: int | None
    reviewer_reasoning: str = ""

    @property
    def is_usable(self) -> bool:
        """True when both credences are present and in range."""
        return (
            self.self_credence is not None
            and self.reviewer_credence is not None
            and MIN_CREDENCE <= self.self_credence <= MAX_CREDENCE
            and MIN_CREDENCE <= self.reviewer_credence <= MAX_CREDENCE
        )

    @property
    def absolute_gap(self) -> int | None:
        """|reviewer - self|, or None if either credence is missing/out-of-range."""
        if not self.is_usable:
            return None
        assert self.self_credence is not None
        assert self.reviewer_credence is not None
        return abs(self.reviewer_credence - self.self_credence)


def compute_calibration_score(
    comparisons: Sequence[CredenceComparison],
) -> float | None:
    """Compute a calibration score in [0, 1] from a set of comparisons.

    For each usable comparison, score = ``1 - |reviewer - self| / 8`` (since
    credences live on 1-9). Returns the mean across usable comparisons, or
    ``None`` if there are no usable comparisons.

    Higher is better; 1.0 means perfect agreement. Samples where either
    credence is missing are skipped (not crashed on).
    """
    usable = [c for c in comparisons if c.is_usable]
    if not usable:
        return None
    per_sample = [1.0 - (c.absolute_gap or 0) / CREDENCE_SPAN for c in usable]
    return sum(per_sample) / len(per_sample)


def classify_calibration(score: float | None) -> str:
    """Bucket a calibration score into a human-readable label."""
    if score is None:
        return "insufficient data"
    if score >= 0.9:
        return "well-calibrated"
    if score >= 0.75:
        return "modestly calibrated"
    if score >= 0.5:
        return "noticeably off"
    return "poorly calibrated"


def overconfidence_delta(
    comparisons: Sequence[CredenceComparison],
) -> float | None:
    """Mean signed (self - reviewer) credence gap.

    Positive means self-reports are systematically higher than reviewer
    (overconfident); negative means systematically lower (underconfident);
    ``None`` when nothing usable.
    """
    usable = [c for c in comparisons if c.is_usable]
    if not usable:
        return None
    gaps = [(c.self_credence or 0) - (c.reviewer_credence or 0) for c in usable]
    return sum(gaps) / len(gaps)


def format_comparisons_markdown(
    comparisons: Sequence[CredenceComparison],
) -> str:
    """Render per-claim comparisons as a Markdown table for the audit trail."""
    if not comparisons:
        return "_No claims sampled._"
    lines = [
        "| Claim | Self | Reviewer | Gap |",
        "|---|---|---|---|",
    ]
    for c in comparisons:
        self_s = "—" if c.self_credence is None else str(c.self_credence)
        rev_s = "—" if c.reviewer_credence is None else str(c.reviewer_credence)
        gap_s = "—" if c.absolute_gap is None else str(c.absolute_gap)
        headline = c.headline.replace("|", "\\|")
        lines.append(f"| `{c.claim_id[:8]}` {headline} | {self_s} | {rev_s} | {gap_s} |")
    return "\n".join(lines)
