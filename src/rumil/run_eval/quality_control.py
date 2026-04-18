"""Quality-control eval helpers.

The quality_control eval agent scans a staged run's outputs for concrete,
glaring errors — broken citations, orphan view items, overconfident claims
without sources, intra-run contradictions, etc. It emits a structured list
of findings (not a free-form critique) so operators can triage quickly.

This module holds:
- the ``QualityControlFinding`` model (one flagged problem)
- ``Severity`` and its mapping to a reputation-event score (negative)
- ``parse_findings_from_report`` — extract a JSON finding block from the
  LLM's markdown report
- ``cap_findings`` — enforce the per-run guard against spray

Design notes
------------

We persist the structured findings as part of the per-dimension row stored
in ``run_eval_reports.dimension_reports`` (JSONB), alongside the
agent's free-form markdown report. That's a column on the existing report
rather than a new ``run_eval_qc_findings`` table. Two reasons:

1. Findings are always queried alongside the report they came from — we
   never need a cross-report "all QC findings in the project" query that
   wouldn't be well-served by unpacking the JSONB at query time.
2. The JSONB column already exists, so no migration churn. Findings are
   an evolving structure; keeping them in JSONB lets us iterate on schema
   without a migration per change.

If cross-report queries ever become hot, promote to a table then.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from enum import Enum

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

MAX_FINDINGS_PER_RUN = 20


class Severity(str, Enum):
    """Severity bucket for a QC finding."""

    LOW = "low"
    MODERATE = "moderate"
    CRITICAL = "critical"


_SEVERITY_SCORES: dict[Severity, float] = {
    Severity.LOW: -0.3,
    Severity.MODERATE: -0.6,
    Severity.CRITICAL: -1.0,
}


def severity_to_score(severity: Severity) -> float:
    """Map a severity bucket to its reputation-event score.

    Scores are negative on purpose — a QC finding is a quality deficit, so
    it should drag the ``quality_control`` dimension's mean score down.
    Humans and future orchestrators can read the raw events at query time.
    """
    return _SEVERITY_SCORES[severity]


class QualityControlFinding(BaseModel):
    """One concrete quality issue flagged by the QC agent."""

    kind: str = Field(
        description=(
            "Short category slug, e.g. factual_error, broken_citation, "
            "orphan_view_item, overconfident_claim, intra_run_contradiction."
        )
    )
    page_ids: list[str] = Field(
        default_factory=list,
        description="Page IDs implicated by this finding (short or full).",
    )
    severity: Severity = Field(description="low | moderate | critical.")
    evidence: str = Field(description="One-sentence quote or paraphrase showing the problem.")
    suggested_fix: str = Field(
        default="",
        description="One-sentence suggested remediation (optional).",
    )


def cap_findings(
    findings: Sequence[QualityControlFinding],
    limit: int = MAX_FINDINGS_PER_RUN,
) -> list[QualityControlFinding]:
    """Trim a list of findings to the per-run cap, critical-first.

    The cap is a guard against a verbose LLM spraying trivial flags. We
    sort by severity (critical > moderate > low) and drop the tail so the
    most important findings always survive.
    """
    order = {Severity.CRITICAL: 0, Severity.MODERATE: 1, Severity.LOW: 2}
    sorted_findings = sorted(findings, key=lambda f: order[f.severity])
    return list(sorted_findings[:limit])


_JSON_BLOCK_RE = re.compile(
    r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```",
    re.DOTALL,
)


def parse_findings_from_report(report_text: str) -> list[QualityControlFinding]:
    """Extract structured findings from the QC agent's markdown report.

    The prompt instructs the agent to emit a fenced JSON block containing
    an object ``{"findings": [...]}`` or a bare array. We parse the first
    such block we find. If none is found or parsing fails, we return an
    empty list — the markdown text still goes to the report, but no
    reputation events fire.
    """
    for match in _JSON_BLOCK_RE.finditer(report_text):
        blob = match.group(1)
        try:
            parsed = json.loads(blob)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and "findings" in parsed:
            raw_list = parsed["findings"]
        elif isinstance(parsed, list):
            raw_list = parsed
        else:
            continue
        findings: list[QualityControlFinding] = []
        for item in raw_list:
            if not isinstance(item, dict):
                continue
            try:
                findings.append(QualityControlFinding.model_validate(item))
            except Exception:
                log.debug("Skipping malformed QC finding: %s", item)
                continue
        if findings:
            return findings
    return []


def format_findings_markdown(findings: Sequence[QualityControlFinding]) -> str:
    """Render findings as a compact Markdown list for the aggregate report."""
    if not findings:
        return "_No quality-control findings flagged._"
    lines = [f"**{len(findings)} finding(s) flagged**", ""]
    for f in findings:
        pages = ", ".join(f"`{p}`" for p in f.page_ids) if f.page_ids else "—"
        lines.append(f"- **[{f.severity.value}]** `{f.kind}` — {f.evidence} (pages: {pages})")
        if f.suggested_fix:
            lines.append(f"  - *Suggested fix:* {f.suggested_fix}")
    return "\n".join(lines)
