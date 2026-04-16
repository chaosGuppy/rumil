"""Report formatting and persistence for single-run evaluations."""

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from rumil.run_eval.agents import EvalAgentSpec

REPORTS_DIR = Path(__file__).resolve().parents[3] / "data" / "run-eval-reports"


def format_run_eval_report(
    agent_reports: Sequence[tuple[EvalAgentSpec, str]],
    run_id: str,
    overall_assessment: str,
) -> str:
    """Format the aggregate report across all agents.

    Each tuple is (spec, report_text).
    """
    lines = [
        "# Run Evaluation Report",
        "",
        f"**Run:** `{run_id}`",
        f"**Date:** {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "## Overall Assessment",
        "",
        overall_assessment,
        "",
        "---",
        "",
    ]

    for spec, report in agent_reports:
        lines.append(f"# {spec.display_name}")
        lines.append("")
        lines.append(report)
        lines.append("")
        lines.append("---")
        lines.append("")

    return "\n".join(lines)


def save_run_eval_report(content: str, run_id: str) -> Path:
    """Save the report to data/run-eval-reports/ and return the path."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    filename = f"{timestamp}-{run_id[:8]}.md"
    path = REPORTS_DIR / filename
    path.write_text(content, encoding="utf-8")
    return path
