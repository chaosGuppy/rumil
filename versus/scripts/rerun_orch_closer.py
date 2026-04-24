"""Re-fire just the orch closer against an existing question page.

Cheap way to test closer-prompt / closer-rendering / closer-tool
changes without paying for another full TwoPhaseOrchestrator cycle.
Reuses the orchestrator's existing staged subtree (considerations,
judgements, views, view_items) — they're already persisted against
the original staged run_id.

Usage:

    uv run python versus/scripts/rerun_orch_closer.py \\
        --run-id <orch-run-id> \\
        --question-id <question-page-id> \\
        --model claude-sonnet-4-6 \\
        --task-name general_quality

Writes a new VERSUS_JUDGE call into the same run so the trace UI
shows it alongside the original closer. Does NOT write a judgment
row to ``judgments.jsonl`` — this is just for inspecting the new
closer's output.
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys

VERSUS_ROOT = pathlib.Path(__file__).resolve().parent.parent
RUMIL_ROOT = VERSUS_ROOT.parent

sys.path.insert(0, str(VERSUS_ROOT / "src"))
sys.path.insert(0, str(RUMIL_ROOT / "src"))

from rumil.database import DB  # noqa: E402
from rumil.settings import override_settings  # noqa: E402
from rumil.versus_bridge import _run_orch_closer, extract_preference, label_to_verdict  # noqa: E402
from rumil.versus_prompts import get_rumil_dimension_body  # noqa: E402


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-id", required=True, help="Original orch run id.")
    ap.add_argument("--question-id", required=True, help="Versus question page id.")
    ap.add_argument(
        "--model",
        required=True,
        help="Anthropic model id (e.g. claude-sonnet-4-6). Same knob as --rumil-model.",
    )
    ap.add_argument(
        "--task-name",
        default="general_quality",
        help="Dimension name for the task body (default: general_quality).",
    )
    args = ap.parse_args()

    task_body = get_rumil_dimension_body(args.task_name)
    # Resolve the original run's project_id so the new closer call can
    # be saved (save_call rejects empty UUIDs). Reusing the existing
    # run_id + staged view means the new call lands alongside the
    # original orch's persisted subtree without any data copying.
    bootstrap = await DB.create(run_id="_rerun_closer_bootstrap", prod=False, staged=False)
    run_row = await bootstrap.get_run(args.run_id)
    if not run_row:
        print(f"[err] run {args.run_id} not found", file=sys.stderr)
        sys.exit(1)
    project_id = run_row["project_id"]
    db = await DB.create(run_id=args.run_id, prod=False, project_id=project_id, staged=True)

    with override_settings(rumil_model_override=args.model):
        report_text, call = await _run_orch_closer(
            db, args.question_id, task_body=task_body, broadcaster=None
        )

    label = extract_preference(report_text)
    verdict = label_to_verdict(label)
    print(f"[closer-call-id] {call.id}")
    print(f"[label] {label}")
    print(f"[verdict] {verdict}")
    print(f"[cost-usd] {call.cost_usd}")
    print()
    print("=== closer report ===")
    print(report_text)


if __name__ == "__main__":
    asyncio.run(main())
