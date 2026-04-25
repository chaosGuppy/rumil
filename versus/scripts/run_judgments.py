"""Run pairwise OpenRouter judgments over completions.jsonl.

Default: run every configured judge × criterion against every pending
pair. Dedup keys hold, so re-runs fill gaps.

Filter flags mirror the rumil-side script so targeted runs are possible
without editing config.yaml:
  --judge-model <id>   (repeatable)  restrict to specific judges
  --criterion <name>   (repeatable)  restrict to specific criteria
  --essay <id>         (repeatable)  restrict to specific essays
  --contestants <csv>                only pairs where both source_ids are in the list
  --vs-human                         only pairs where one side is "human"
  --limit N                          cap on number of judgment calls
  --dry-run                          print the plan and exit
"""

from __future__ import annotations

import argparse
import pathlib
import sys

VERSUS_ROOT = pathlib.Path(__file__).resolve().parent.parent
RUMIL_ROOT = VERSUS_ROOT.parent

# versus/src for the versus package; rumil/src for ``rumil.versus_bridge``
# which judge.py now imports for the shared judge prompt.
sys.path.insert(0, str(VERSUS_ROOT / "src"))
sys.path.insert(0, str(RUMIL_ROOT / "src"))

# Line-buffer stdout so backgrounded runs (`... > logfile 2>&1 &`) show
# progress in real time instead of dumping on exit.
sys.stdout.reconfigure(line_buffering=True)  # pyright: ignore[reportAttributeAccessIssue]

from versus import config, judge, prepare  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=str(VERSUS_ROOT / "config.yaml"))
    ap.add_argument(
        "--judge-model",
        action="append",
        default=None,
        help="OpenRouter judge model id, repeatable. Overrides judging.models.",
    )
    ap.add_argument(
        "--criterion",
        action="append",
        default=None,
        help="Criterion name, repeatable. Overrides judging.criteria.",
    )
    ap.add_argument(
        "--essay",
        action="append",
        default=None,
        help="Restrict to specified essay_id(s). Repeatable.",
    )
    ap.add_argument(
        "--active",
        action="store_true",
        help=(
            "Restrict to the canonical active set: current schema_version "
            "and not in cfg.essays.exclude_ids. Same gate /versus applies. "
            "Composes with --essay (intersected)."
        ),
    )
    ap.add_argument(
        "--contestants",
        default=None,
        help=("Comma-separated source_ids; only emit pairs where both sides are in this list."),
    )
    ap.add_argument(
        "--vs-human",
        action="store_true",
        help="Only emit pairs where one side is 'human'.",
    )
    ap.add_argument(
        "--current-only",
        action="store_true",
        help=(
            "Skip groups whose prefix_config_hash isn't the current one for "
            "the essay (i.e. they reference older essay markdown). Without "
            "this, completions tied to older imports also get judged."
        ),
    )
    ap.add_argument("--limit", type=int, default=None, help="Cap on number of judgments.")
    ap.add_argument("--dry-run", action="store_true", help="Print the plan and exit.")
    args = ap.parse_args()

    cfg = config.load(args.config)
    # Anchor relative paths to versus/ so the script works regardless of cwd
    # (e.g. when invoked from the rumil root via uv run).
    for field in ("completions_log", "judgments_log", "paraphrases_log"):
        p = getattr(cfg.storage, field)
        if not p.is_absolute():
            setattr(cfg.storage, field, VERSUS_ROOT / p)
    if not cfg.essays.cache_dir.is_absolute():
        cfg.essays.cache_dir = VERSUS_ROOT / cfg.essays.cache_dir
    contestants = (
        [s.strip() for s in args.contestants.split(",") if s.strip()] if args.contestants else None
    )
    if args.active:
        active = prepare.active_essay_ids(cfg.essays.cache_dir, cfg.essays.exclude_ids)
        essay_ids = sorted(active & set(args.essay)) if args.essay else sorted(active)
    else:
        essay_ids = args.essay
    judge.run(
        cfg,
        judge_models=args.judge_model,
        criteria=args.criterion,
        essay_ids=essay_ids,
        contestants=contestants,
        vs_human=args.vs_human,
        current_only=args.current_only,
        limit=args.limit,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
