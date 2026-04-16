"""Report formatting and persistence for A/B evaluations."""

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from rumil.run_eval.agents import EvalAgentSpec

REPORTS_DIR = Path(__file__).resolve().parents[3] / "data" / "ab-reports"


def format_agent_report(
    spec: EvalAgentSpec,
    report_a: str,
    report_b: str,
    comparison: str,
    preference: str,
) -> str:
    """Format a single agent's full evaluation (both arms + comparison)."""
    return (
        f"# {spec.display_name}\n\n"
        f"## Run A Report\n\n{report_a}\n\n"
        f"## Run B Report\n\n{report_b}\n\n"
        "## Comparison\n\n"
        f"**Preference: {preference}**\n\n"
        f"{comparison}\n"
    )


def format_aggregate_report(
    agent_reports: Sequence[tuple[EvalAgentSpec, str, str, str, str]],
    run_id_a: str,
    run_id_b: str,
    overall_assessment: str,
) -> str:
    """Format the aggregate report across all agents.

    Each tuple is (spec, report_a, report_b, comparison, preference).
    """
    lines = [
        "# A/B Evaluation Report",
        "",
        f"**Run A:** `{run_id_a}`",
        f"**Run B:** `{run_id_b}`",
        f"**Date:** {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "## Preference Summary",
        "",
        "| Dimension | Preference |",
        "|-----------|------------|",
    ]
    for spec, _ra, _rb, _comp, preference in agent_reports:
        lines.append(f"| {spec.display_name} | {preference} |")

    lines.append("")
    lines.append("## Overall Assessment")
    lines.append("")
    lines.append(overall_assessment)
    lines.append("")
    lines.append("---")
    lines.append("")

    for spec, report_a, report_b, comparison, preference in agent_reports:
        lines.append(
            format_agent_report(spec, report_a, report_b, comparison, preference)
        )
        lines.append("---")
        lines.append("")

    return "\n".join(lines)


def save_ab_report(content: str, run_id_a: str, run_id_b: str) -> Path:
    """Save the aggregate report to data/ab-reports/ and return the path."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    filename = f"{timestamp}-{run_id_a[:8]}-vs-{run_id_b[:8]}.md"
    path = REPORTS_DIR / filename
    path.write_text(content, encoding="utf-8")
    return path
