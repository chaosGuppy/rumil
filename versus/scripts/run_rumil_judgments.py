"""Run pairwise versus judgments with Anthropic (the rumil-style backend).

Mirrors scripts/run_judgments.py but calls Anthropic directly instead of
OpenRouter. Writes into the same data/judgments.jsonl with
judge_model = 'anthropic:<model>', so the /results UI grid surfaces these
alongside OpenRouter judges.

Env resolution for ANTHROPIC_API_KEY (and OPENROUTER_API_KEY too, though
this script doesn't need it): versus/.env, then <rumil-root>/.env, then
the process environment. See versus/CLAUDE.md.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

VERSUS_ROOT = pathlib.Path(__file__).resolve().parent.parent
RUMIL_ROOT = VERSUS_ROOT.parent

sys.path.insert(0, str(VERSUS_ROOT / "src"))

from versus import envcascade  # noqa: E402

envcascade.apply(
    ("ANTHROPIC_API_KEY", "OPENROUTER_API_KEY"),
    versus_root=VERSUS_ROOT,
    rumil_root=RUMIL_ROOT,
)

from versus import config, rumil_judge  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=str(VERSUS_ROOT / "config.yaml"))
    ap.add_argument(
        "--model",
        action="append",
        default=None,
        help="Anthropic model id, repeatable. Overrides judging.anthropic_models.",
    )
    ap.add_argument("--limit", type=int, default=None, help="Cap on number of judgments.")
    ap.add_argument("--dry-run", action="store_true", help="List pending keys and exit.")
    args = ap.parse_args()

    cfg = config.load(args.config)
    for field in ("completions_log", "judgments_log", "paraphrases_log"):
        p = getattr(cfg.storage, field)
        if not p.is_absolute():
            setattr(cfg.storage, field, VERSUS_ROOT / p)
    models = args.model if args.model else list(cfg.judging.anthropic_models)
    rumil_judge.run(cfg, models, limit=args.limit, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
