"""Run pairwise versus judgments against rumil-style judge backends.

Three variants, selected via ``--variant``:

- ``text`` (default): single-turn Anthropic call using the versus judge
  prompt. No rumil imports, no DB. Fast and cheap.

- ``ws``: one VERSUS_JUDGE agent call per pair via rumil's SDK agent,
  with single-arm workspace-exploration tools against a user-chosen
  rumil workspace. Requires ``--workspace`` + running Supabase.

- ``orch``: full TwoPhaseOrchestrator run per pair + closing call.
  Expensive. Requires ``--workspace`` + running Supabase.

Env resolution for ANTHROPIC_API_KEY / OPENROUTER_API_KEY: versus/.env,
then <rumil-root>/.env, then the process environment.
See versus/CLAUDE.md for details.
"""

from __future__ import annotations

import argparse
import asyncio
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

DEFAULT_DIMENSIONS = ("general_quality",)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=str(VERSUS_ROOT / "config.yaml"))
    ap.add_argument(
        "--variant",
        choices=("text", "ws", "orch"),
        default="text",
        help="Which rumil judge path to run (default: text).",
    )
    ap.add_argument(
        "--model",
        action="append",
        default=None,
        help=(
            "Anthropic model id, repeatable. text variant only -- ws/orch "
            "variants use rumil's configured model. Overrides "
            "judging.anthropic_models."
        ),
    )
    ap.add_argument(
        "--workspace",
        default=None,
        help="Rumil workspace (project) name for ws/orch variants (e.g. 'redwood').",
    )
    ap.add_argument(
        "--dimension",
        action="append",
        default=None,
        help=(
            "Essay-adapted rumil dimension name, repeatable "
            "(default: general_quality). Requires a prompt at "
            "prompts/versus-<name>.md."
        ),
    )
    ap.add_argument(
        "--versus-criterion",
        action="append",
        default=None,
        help=(
            "Versus criterion name, repeatable. Adds versus-criterion "
            "tasks alongside rumil dimensions for direct comparison with "
            "OpenRouter judges. Judge-model strings get a 'versus_' prefix "
            "so dedup keys differ from dimension-based rows."
        ),
    )
    ap.add_argument(
        "--budget",
        type=int,
        default=1,
        help="Orchestrator research-call budget per pair (orch variant only). Default: 1.",
    )
    ap.add_argument(
        "--concurrency",
        type=int,
        default=2,
        help="Concurrent ws-variant judgments (default: 2).",
    )
    ap.add_argument("--limit", type=int, default=None, help="Cap on number of judgments.")
    ap.add_argument("--dry-run", action="store_true", help="List pending keys and exit.")
    args = ap.parse_args()

    cfg = config.load(args.config)
    for field in ("completions_log", "judgments_log", "paraphrases_log"):
        p = getattr(cfg.storage, field)
        if not p.is_absolute():
            setattr(cfg.storage, field, VERSUS_ROOT / p)

    if args.variant == "text":
        models = args.model if args.model else list(cfg.judging.anthropic_models)
        rumil_judge.run(cfg, models, limit=args.limit, dry_run=args.dry_run)
        return

    if not args.workspace:
        ap.error(f"--workspace is required for --variant {args.variant}")
    dimensions = tuple(args.dimension) if args.dimension else DEFAULT_DIMENSIONS
    versus_criteria = tuple(args.versus_criterion) if args.versus_criterion else ()

    if args.variant == "ws":
        asyncio.run(
            rumil_judge.run_ws(
                cfg,
                workspace=args.workspace,
                dimensions=dimensions,
                versus_criteria=versus_criteria,
                limit=args.limit,
                dry_run=args.dry_run,
                concurrency=args.concurrency,
            )
        )
    elif args.variant == "orch":
        asyncio.run(
            rumil_judge.run_orch(
                cfg,
                workspace=args.workspace,
                dimensions=dimensions,
                versus_criteria=versus_criteria,
                budget=args.budget,
                limit=args.limit,
                dry_run=args.dry_run,
            )
        )


if __name__ == "__main__":
    main()
