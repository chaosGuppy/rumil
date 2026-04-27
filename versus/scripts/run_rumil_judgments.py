"""Run pairwise versus judgments.

Three modes:

- **Blind** (default, no ``--variant``): single-turn LLM call with the
  blind shell — no tools, no DB, no workspace. Each ``--model`` is
  routed: claude-* go direct to Anthropic, anything else through
  OpenRouter. This subsumes the previous ``text`` / ``rumil-text`` /
  OpenRouter judge paths into one entry point.

- ``--variant ws``: one VERSUS_JUDGE agent call per pair via rumil's
  SDK agent, with single-arm workspace-exploration tools against a
  user-chosen rumil workspace. Requires ``--workspace`` + running
  Supabase.

- ``--variant orch``: full TwoPhaseOrchestrator run per pair + closing
  call. Expensive. Requires ``--workspace`` + running Supabase.

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

# versus/src for the versus package; rumil/src for the bridge / DB / etc.
sys.path.insert(0, str(VERSUS_ROOT / "src"))
sys.path.insert(0, str(RUMIL_ROOT / "src"))

from versus import envcascade  # noqa: E402

envcascade.apply(
    ("ANTHROPIC_API_KEY", "OPENROUTER_API_KEY"),
    versus_root=VERSUS_ROOT,
    rumil_root=RUMIL_ROOT,
)

from rumil.settings import RUMIL_MODEL_ALIASES, resolve_model_alias  # noqa: E402
from versus import config, judge, prepare, rumil_judge  # noqa: E402

DEFAULT_DIMENSIONS = ("general_quality",)


def main() -> None:
    # Enable line-buffering on stdout so progress prints land in the
    # logfile immediately when this script is backgrounded with
    # `... > logfile 2>&1`. Without this, Python block-buffers (~8 KiB)
    # against the redirected file and `[plan]` / `[run]` / `[done]`
    # lines sit invisible for the duration of a long run.
    sys.stdout.reconfigure(line_buffering=True)  # pyright: ignore[reportAttributeAccessIssue]

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=str(VERSUS_ROOT / "config.yaml"))
    ap.add_argument(
        "--variant",
        choices=("ws", "orch"),
        default=None,
        help=(
            "Tool-using variant. Omit for the default blind judge path. "
            "ws/orch require --workspace and running Supabase."
        ),
    )
    ap.add_argument(
        "--model",
        action="append",
        default=None,
        help=(
            "Model selection. Accepts a short alias "
            f"({'/'.join(RUMIL_MODEL_ALIASES)}), a bare Anthropic id "
            "(claude-*), or an OpenRouter id (provider/model). For the "
            "default blind path: repeat to judge with multiple models; "
            "claude-* go direct to Anthropic, others via OpenRouter; "
            "defaults to cfg.judging.models. For ws/orch: pass at most "
            "one (default: opus)."
        ),
    )
    ap.add_argument(
        "--workspace",
        default=None,
        help="Rumil workspace (project) name for ws/orch variants. Required for those variants; no default.",
    )
    ap.add_argument(
        "--dimension",
        action="append",
        default=None,
        help=(
            "Essay-adapted rumil dimension name, repeatable "
            "(default: general_quality). Requires a prompt at "
            "src/rumil/prompts/versus-<name>.md."
        ),
    )
    ap.add_argument(
        "--budget",
        type=int,
        default=4,
        help=(
            "Orchestrator research-call budget per pair (orch variant only). "
            "TwoPhaseOrchestrator requires a minimum of 4. Default: 4."
        ),
    )
    ap.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help=(
            "Concurrent judgments per LLM call. If unset, the variant picks "
            "its own default: blind path = cfg.per_model_concurrency per "
            "model (usually 8); ws = 2."
        ),
    )
    ap.add_argument("--limit", type=int, default=None, help="Cap on number of judgments.")
    ap.add_argument("--dry-run", action="store_true", help="List pending keys and exit.")
    ap.add_argument(
        "--essay",
        action="append",
        default=None,
        help="Restrict planning to specified essay_id(s). Repeatable. ws/orch only.",
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
        help=(
            "Comma-separated source_ids; only emit pairs where both sides "
            "are in this list. ws/orch only."
        ),
    )
    ap.add_argument(
        "--vs-human",
        action="store_true",
        help="Only emit pairs where one side is 'human'. ws/orch only.",
    )
    ap.add_argument(
        "--current-only",
        action="store_true",
        help=(
            "Skip groups whose prefix_config_hash isn't the current one for "
            "the essay. Without this, pairs against older essay imports also "
            "get judged."
        ),
    )
    ap.add_argument(
        "--prefix-label",
        default=None,
        help=(
            "Scope --current-only to a specific prefix variant (default: "
            "the canonical `prefix:` entry). Sibling variants live under "
            "`prefix_variants:` in config.yaml; pass their `id`."
        ),
    )
    ap.add_argument(
        "--persist",
        action="store_true",
        help=(
            "Persist versus-created pages to the workspace baseline instead "
            "of staging them (ws/orch only). Default is staged: the agent "
            "still reads baseline workspace material but its Question pages "
            "and (for orch) research subtree are scoped to the run's staged "
            "view -- invisible to other readers of the workspace."
        ),
    )
    args = ap.parse_args()

    contestants = (
        [s.strip() for s in args.contestants.split(",") if s.strip()] if args.contestants else None
    )

    cfg = config.load(args.config)
    for field in ("completions_log", "judgments_log", "paraphrases_log"):
        p = getattr(cfg.storage, field)
        if not p.is_absolute():
            setattr(cfg.storage, field, VERSUS_ROOT / p)
    if not cfg.essays.cache_dir.is_absolute():
        cfg.essays.cache_dir = VERSUS_ROOT / cfg.essays.cache_dir

    if args.active:
        active = prepare.active_essay_ids(cfg.essays.cache_dir, cfg.essays.exclude_ids)
        essay_ids = sorted(active & set(args.essay)) if args.essay else sorted(active)
    else:
        essay_ids = args.essay

    prefix_cfg = prepare.resolve_prefix_cfg(cfg, args.prefix_label)
    print(f"[prefix] using variant {prefix_cfg.id!r}")

    if args.variant is None:
        # Blind path: route claude-* direct to Anthropic, others via OpenRouter.
        if args.model:
            models = [resolve_model_alias(m) for m in args.model]
        else:
            models = list(cfg.judging.models)
        if not models:
            ap.error("no models passed and cfg.judging.models is empty")
        dimensions = tuple(args.dimension) if args.dimension else DEFAULT_DIMENSIONS
        judge.run_blind(
            cfg,
            models=models,
            dimensions=dimensions,
            limit=args.limit,
            dry_run=args.dry_run,
            essay_ids=essay_ids,
            contestants=contestants,
            vs_human=args.vs_human,
            current_only=args.current_only,
            prefix_cfg=prefix_cfg,
        )
        return

    if args.model and len(args.model) > 1:
        ap.error(
            f"--variant {args.variant} takes at most one --model "
            f"(got {len(args.model)}: {args.model})"
        )
    model_id = resolve_model_alias(args.model[0]) if args.model else RUMIL_MODEL_ALIASES["opus"]

    if not args.workspace:
        ap.error(f"--workspace is required for --variant {args.variant}")

    dimensions = tuple(args.dimension) if args.dimension else DEFAULT_DIMENSIONS

    if args.variant == "ws":
        asyncio.run(
            rumil_judge.run_ws(
                cfg,
                workspace=args.workspace,
                model=model_id,
                dimensions=dimensions,
                limit=args.limit,
                dry_run=args.dry_run,
                concurrency=args.concurrency,
                essay_ids=essay_ids,
                contestants=contestants,
                vs_human=args.vs_human,
                current_only=args.current_only,
                prefix_cfg=prefix_cfg,
                persist=args.persist,
            )
        )
    elif args.variant == "orch":
        asyncio.run(
            rumil_judge.run_orch(
                cfg,
                workspace=args.workspace,
                model=model_id,
                dimensions=dimensions,
                budget=args.budget,
                limit=args.limit,
                dry_run=args.dry_run,
                concurrency=args.concurrency,
                essay_ids=essay_ids,
                contestants=contestants,
                vs_human=args.vs_human,
                current_only=args.current_only,
                prefix_cfg=prefix_cfg,
                persist=args.persist,
            )
        )


if __name__ == "__main__":
    main()
