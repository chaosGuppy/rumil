"""Run pairwise versus judgments.

Two modes:

- **Blind** (default, no ``--variant``): single-turn LLM call with the
  blind shell — no tools, no DB, no workspace. Each ``--model`` is
  routed: claude-* go direct to Anthropic, anything else through
  OpenRouter. This subsumes the previous ``text`` / ``rumil-text`` /
  OpenRouter judge paths into one entry point.

- ``--variant orch``: full TwoPhaseOrchestrator run per pair + closing
  call. Expensive. Requires ``--workspace`` + running Supabase.

The earlier ``--variant ws`` path was removed; a low-budget orch run
covers the agentic-baseline use case. Historical ``rumil:ws:*`` rows
in ``versus_judgments`` are preserved.

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

try:
    import rumil  # noqa: F401
except ModuleNotFoundError:
    sys.stderr.write(
        "[err] rumil isn't importable from this venv. Run from the rumil "
        "repo root, not versus/:\n"
        f"      cd {RUMIL_ROOT} && uv run python versus/scripts/run_rumil_judgments.py ...\n"
    )
    raise SystemExit(1) from None

from versus import envcascade  # noqa: E402

envcascade.apply(
    (
        "ANTHROPIC_API_KEY",
        "OPENROUTER_API_KEY",
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
        "LANGFUSE_HOST",
        "LANGFUSE_BASE_URL",
    ),
    versus_root=VERSUS_ROOT,
    rumil_root=RUMIL_ROOT,
)

from rumil.settings import RUMIL_MODEL_ALIASES, resolve_model_alias  # noqa: E402
from versus import config, judge, prepare, rumil_judge, versus_db  # noqa: E402

DEFAULT_DIMENSIONS = ("would_recommend",)


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
        choices=("orch", "reflective", "simple_spine"),
        default=None,
        help=(
            "Workflow variant. Omit for the default blind judge path. "
            "orch fires TwoPhaseOrchestrator (research + closer); "
            "reflective fires ReflectiveJudgeWorkflow (read → reflect → "
            "verdict, no research); simple_spine fires SimpleSpineWorkflow "
            "(structured-rounds main agent loop with parallel subroutine "
            "spawns). All require --workspace and running Supabase."
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
            "defaults to cfg.judging.models. For orch: pass at most "
            "one (default: opus)."
        ),
    )
    ap.add_argument(
        "--workspace",
        default="versus",
        help="Rumil workspace (project) name for the orch variant. Default: versus.",
    )
    ap.add_argument(
        "--dimension",
        action="append",
        default=None,
        help=(
            "Essay-adapted rumil dimension name, repeatable "
            "(default: would_recommend). Requires a prompt at "
            "src/rumil/prompts/versus-<name>.md."
        ),
    )
    ap.add_argument(
        "--budget",
        type=int,
        default=None,
        help=(
            "Orchestrator research-call budget per pair (orch variant only). "
            "TwoPhaseOrchestrator requires a minimum of 4. Required for "
            "--variant orch; rejected for simple_spine (use --simple-spine-budget-tokens)."
        ),
    )
    ap.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help=(
            "Concurrent judgments per LLM call. If unset, the variant picks "
            "its own default: blind path = cfg.per_model_concurrency per "
            "model (usually 8); orch = 1 (serial)."
        ),
    )
    ap.add_argument("--limit", type=int, default=None, help="Cap on number of judgments.")
    ap.add_argument("--dry-run", action="store_true", help="List pending keys and exit.")
    ap.add_argument(
        "--essay",
        action="append",
        default=None,
        help="Restrict planning to specified essay_id(s). Repeatable. Honored on blind and orch.",
    )
    ap.add_argument(
        "--include-stale",
        action="store_true",
        help=(
            "Default behavior is the canonical active set (current "
            "schema_version, not in cfg.essays.exclude_ids — same gate "
            "/versus applies). Pass this to plan against every essay "
            "with rows in versus_texts instead, including off-feed or "
            "old-schema rows. Composes with --essay (intersected)."
        ),
    )
    ap.add_argument(
        "--contestants",
        default=None,
        help=(
            "Comma-separated source_ids; only emit pairs where both sides "
            "are in this list. Honored on blind and orch."
        ),
    )
    ap.add_argument(
        "--vs-human",
        action="store_true",
        help="Only emit pairs where one side is 'human'. orch only.",
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
        action="append",
        default=None,
        help=(
            "Restrict planning to a specific prefix variant. Repeatable on "
            "the blind path: pass once per variant to plan multiple in one "
            "invocation. When set, only rows whose prefix_hash matches that "
            "variant's current hash are enumerated — stale rows under that "
            "variant are excluded too. Without this flag, every prefix_hash "
            "present in versus_texts is eligible. Sibling variants live "
            "under `prefix_variants:` in config.yaml; pass their `id`. "
            "orch currently takes at most one --prefix-label."
        ),
    )
    ap.add_argument(
        "--prod",
        action="store_true",
        help=(
            "Target the production Supabase database for versus_texts / "
            "versus_judgments AND, on orch, the rumil workspace + run "
            "tables (default: local for both). On orch, the workspace "
            "named via --workspace must already exist on the target DB "
            "(typo protection — create via rumil main.py first). orch "
            "runs are still staged by default; pass --persist to write the "
            "per-pair Question + research subtree to baseline."
        ),
    )
    ap.add_argument(
        "--persist",
        action="store_true",
        help=(
            "Persist versus-created pages to the workspace baseline instead "
            "of staging them (orch only). Default is staged: the agent "
            "still reads baseline workspace material but its Question pages "
            "and the orch research subtree are scoped to the run's staged "
            "view -- invisible to other readers of the workspace."
        ),
    )
    # Reflective-variant per-stage knobs. All optional; default None
    # inherits the workflow's built-in prompts and the bridge-set
    # rumil_model_override for the model. Ignored on --variant orch.
    ap.add_argument(
        "--reader-model",
        default=None,
        help="Reflective: override the read-stage model. Anthropic id or short alias.",
    )
    ap.add_argument(
        "--reflector-model",
        default=None,
        help="Reflective: override the reflect-stage model.",
    )
    ap.add_argument(
        "--verdict-model",
        default=None,
        help="Reflective: override the verdict-stage model.",
    )
    ap.add_argument(
        "--read-prompt-path",
        default=None,
        help=(
            "Reflective: path to a markdown file replacing the built-in "
            "read prompt. Loaded at construction; the loaded text is "
            "what fingerprints. Empty / whitespace-only files are rejected."
        ),
    )
    ap.add_argument(
        "--reflect-prompt-path",
        default=None,
        help="Reflective: path to a markdown file replacing the built-in reflect prompt.",
    )
    ap.add_argument(
        "--verdict-prompt-path",
        default=None,
        help="Reflective: path to a markdown file replacing the built-in verdict prompt.",
    )
    ap.add_argument(
        "--simple-spine-config-name",
        default="judge_pair",
        help=(
            "simple_spine: preset name resolved via "
            "rumil.orchestrators.simple_spine.presets.get_preset. "
            "Default: judge_pair."
        ),
    )
    ap.add_argument(
        "--simple-spine-budget-tokens",
        type=int,
        default=None,
        help=(
            "simple_spine: raw token cap on the run (the only hard "
            "terminator). Required for --variant simple_spine. "
            "SimpleSpine has no budget-unit primitive, so this is the "
            "direct token count — pass e.g. 200000."
        ),
    )
    args = ap.parse_args()

    contestants = (
        [s.strip() for s in args.contestants.split(",") if s.strip()] if args.contestants else None
    )

    cfg = config.load(args.config)
    if not cfg.storage.paraphrases_log.is_absolute():
        cfg.storage.paraphrases_log = VERSUS_ROOT / cfg.storage.paraphrases_log
    if not cfg.essays.cache_dir.is_absolute():
        cfg.essays.cache_dir = VERSUS_ROOT / cfg.essays.cache_dir

    if args.include_stale:
        essay_ids = args.essay
    else:
        active = prepare.active_essay_ids(
            cfg.essays.exclude_ids, client=versus_db.get_client(prod=args.prod)
        )
        essay_ids = sorted(active & set(args.essay)) if args.essay else sorted(active)

    prefix_cfgs = (
        [prepare.resolve_prefix_cfg(cfg, label) for label in args.prefix_label]
        if args.prefix_label
        else None
    )
    if prefix_cfgs is not None:
        print(f"[prefix] restricting to variants {[p.id for p in prefix_cfgs]!r}")

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
            prefix_cfgs=prefix_cfgs,
            prod=args.prod,
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

    if prefix_cfgs is not None and len(prefix_cfgs) > 1:
        ap.error(
            f"--variant {args.variant} takes at most one --prefix-label "
            f"(got {len(prefix_cfgs)}). Run separately per prefix variant."
        )
    prefix_cfg_one = prefix_cfgs[0] if prefix_cfgs else None

    dimensions = tuple(args.dimension) if args.dimension else DEFAULT_DIMENSIONS

    # Reflective-only flags must be unset on non-reflective paths so
    # the CLI surface stays unambiguous about which variant they apply
    # to. (Symmetric: simple_spine flags would be silently ignored on
    # reflective/orch paths since they don't reach the bridge; not
    # gated here because the default for simple-spine-config-name is
    # the safe sentinel "judge_pair" which is harmless on other paths.)
    if args.variant in ("orch", "simple_spine"):
        for flag, value in (
            ("--reader-model", args.reader_model),
            ("--reflector-model", args.reflector_model),
            ("--verdict-model", args.verdict_model),
            ("--read-prompt-path", args.read_prompt_path),
            ("--reflect-prompt-path", args.reflect_prompt_path),
            ("--verdict-prompt-path", args.verdict_prompt_path),
        ):
            if value is not None:
                ap.error(f"{flag} is only valid with --variant reflective")

    if args.variant == "simple_spine":
        if args.budget is not None:
            ap.error(
                "--variant simple_spine uses raw tokens; pass "
                "--simple-spine-budget-tokens (not --budget). SimpleSpine "
                "has no budget-unit primitive."
            )
        if args.simple_spine_budget_tokens is None:
            ap.error("--variant simple_spine requires --simple-spine-budget-tokens <int>")
    elif args.variant == "orch":
        if args.simple_spine_budget_tokens is not None:
            ap.error("--simple-spine-budget-tokens is only valid with --variant simple_spine")
        if args.budget is None:
            ap.error("--variant orch requires --budget <int>")

    reader_model = resolve_model_alias(args.reader_model) if args.reader_model else None
    reflector_model = resolve_model_alias(args.reflector_model) if args.reflector_model else None
    verdict_model = resolve_model_alias(args.verdict_model) if args.verdict_model else None

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
            prefix_cfg=prefix_cfg_one,
            persist=args.persist,
            prod=args.prod,
            variant=args.variant,
            reader_model=reader_model,
            reflector_model=reflector_model,
            verdict_model=verdict_model,
            read_prompt_path=args.read_prompt_path,
            reflect_prompt_path=args.reflect_prompt_path,
            verdict_prompt_path=args.verdict_prompt_path,
            simple_spine_config_name=args.simple_spine_config_name,
            simple_spine_budget_tokens=args.simple_spine_budget_tokens,
        )
    )


if __name__ == "__main__":
    main()
