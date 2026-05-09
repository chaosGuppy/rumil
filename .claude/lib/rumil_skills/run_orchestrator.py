"""Run the rumil orchestrator against an existing question from Claude Code.

This is the CC-initiated equivalent of ``main.py --continue <id> --budget N``
and backs the ``/rumil-orchestrate`` skill. The orchestrator dispatches a
*sequence* of calls (prioritize, scout, find-considerations, assess, etc.)
until the budget is consumed. This is the multi-call sibling of
``/rumil-dispatch`` — use this when the user wants real research done, not
a single targeted call.

Unlike ``/rumil-dispatch`` (which uses the default staged=True), this runs
with ``staged=False`` so the resulting pages are visible in the baseline
workspace immediately, the same way a ``main.py`` run would be.

Usage:
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.run_orchestrator \\
        <question_id> [--budget N] [--smoke-test] [--workspace NAME] \\
        [--name TEXT]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from dataclasses import replace
from pathlib import Path

from rumil.models import PageType
from rumil.orchestrators import Orchestrator
from rumil.orchestrators.simple_spine import (
    BudgetSpec,
    OrchInputs,
    SimpleSpineOrchestrator,
    discover_configs,
    load_simple_spine_config,
)
from rumil.orchestrators.simple_spine.config import apply_model_override
from rumil.settings import get_settings

from ._format import print_event, print_trace, truncate
from ._runctx import make_db, open_run

DEFAULT_BUDGET = 10
DEFAULT_SIMPLE_SPINE_MAX_TOKENS = 200_000
ORCHESTRATOR_CHOICES = ("two_phase", "experimental", "simple_spine")
SIMPLE_SPINE_CONFIGS_DIR = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "rumil"
    / "orchestrators"
    / "simple_spine"
    / "configs"
)


def _list_simple_spine_configs() -> list[str]:
    return sorted(p.stem for p in discover_configs(SIMPLE_SPINE_CONFIGS_DIR))


def _keys(answer: object) -> list[str]:
    if isinstance(answer, dict):
        return list(answer.keys())
    dumped = getattr(answer, "model_dump", None)
    if callable(dumped):
        return list(dumped().keys())
    return []


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "question_id",
        help="Full or short (8-char) question ID",
    )
    parser.add_argument(
        "--budget",
        type=int,
        default=DEFAULT_BUDGET,
        help=f"Research call budget (default: {DEFAULT_BUDGET})",
    )
    parser.add_argument("--workspace", default=None)
    parser.add_argument(
        "--orchestrator",
        choices=ORCHESTRATOR_CHOICES,
        default=None,
        help=(
            "Which research-loop orchestrator to run. Sets "
            "settings.prioritizer_variant for this invocation. Defaults to "
            "whatever the settings have (typically 'two_phase')."
        ),
    )
    parser.add_argument(
        "--global-prio",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Force settings.enable_global_prio on or off for this invocation "
            "(overrides the ENABLE_GLOBAL_PRIO env var / .env default). "
            "When enabled, GlobalPrioOrchestrator wraps the chosen prioritizer "
            "variant. Omit to inherit the env/settings default."
        ),
    )
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="Faster/cheaper model, fewer rounds (for testing)",
    )
    parser.add_argument(
        "--name",
        default="",
        help="Optional run name (defaults to question headline)",
    )
    parser.add_argument(
        "--config",
        default=None,
        help=(
            "SimpleSpine config preset name (e.g. research, view_freeform). "
            "Required when --orchestrator simple_spine. "
            f"Available: {', '.join(_list_simple_spine_configs())}."
        ),
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=DEFAULT_SIMPLE_SPINE_MAX_TOKENS,
        help=(
            "Token budget for SimpleSpine (default: "
            f"{DEFAULT_SIMPLE_SPINE_MAX_TOKENS}). Ignored unless "
            "--orchestrator simple_spine."
        ),
    )
    parser.add_argument(
        "--model",
        default=None,
        help=(
            "Override every model reference in the SimpleSpine config "
            "(main_model + each subroutine's model + nested-orch presets' "
            "main_model) with this value. Smoke-test convenience for "
            "running a single-model trace end-to-end (e.g. "
            "claude-haiku-4-5-20251001). Ignored unless --orchestrator "
            "simple_spine."
        ),
    )
    parser.add_argument(
        "--no-compaction",
        action="store_true",
        help=(
            "Force-disable enable_server_compaction on the SimpleSpine "
            "config. Required when overriding to a model that doesn't "
            "support compact_20260112 (e.g. Haiku). Ignored unless "
            "--orchestrator simple_spine."
        ),
    )
    args = parser.parse_args()

    if args.smoke_test:
        get_settings().rumil_smoke_test = "1"
    if args.orchestrator:
        get_settings().prioritizer_variant = args.orchestrator
    if args.global_prio is not None:
        get_settings().enable_global_prio = args.global_prio

    is_simple_spine = args.orchestrator == "simple_spine"
    if is_simple_spine and not args.config:
        parser.error("--orchestrator simple_spine requires --config <preset>")
    if not is_simple_spine and (args.config or args.model):
        parser.error("--config / --model only apply with --orchestrator simple_spine")

    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s %(name)s %(message)s",
    )

    # staged=False so orchestrator output is visible to the baseline
    # workspace the same way main.py --continue would leave it.
    db, ws = await make_db(workspace=args.workspace, staged=False)
    try:
        full_id = await db.resolve_page_id(args.question_id)
        if not full_id:
            print(f"no question matching {args.question_id!r} in workspace {ws!r}")
            sys.exit(1)
        page = await db.get_page(full_id)
        if page is None:
            print(f"page {full_id[:8]} vanished mid-lookup")
            sys.exit(1)
        if page.page_type != PageType.QUESTION:
            print(f"error: page {full_id[:8]} is a {page.page_type.value}, not a question")
            sys.exit(1)
        if page.project_id and page.project_id != db.project_id:
            db.project_id = page.project_id

        settings = get_settings()
        if is_simple_spine:
            cfg = load_simple_spine_config(SIMPLE_SPINE_CONFIGS_DIR / f"{args.config}.yaml")
            if args.model:
                cfg = apply_model_override(cfg, args.model)
                # Also stash on settings so nested orchs (deep_dive's
                # simple_spine_recurse) pick up the override on their
                # freshly-loaded preset.
                get_settings().simple_spine_model_override = args.model
            if args.no_compaction:
                cfg = replace(cfg, enable_server_compaction=False)
            print(f"workspace:    {ws}")
            print(f"question:     {full_id[:8]}  {truncate(page.headline, 80)}")
            print(
                f"orchestrator: simple_spine[{args.config}]  "
                f"model={cfg.main_model}  max_tokens={args.max_tokens}"
            )
            extra_config = {
                "smoke_test": bool(args.smoke_test),
                "simple_spine_config": args.config,
                "simple_spine_fingerprint": cfg.fingerprint_short,
                "simple_spine_max_tokens": args.max_tokens,
                "simple_spine_main_model": cfg.main_model,
            }
            await open_run(
                db,
                name=args.name or page.headline,
                question_id=full_id,
                skill="rumil-orchestrate",
                budget=args.budget,
                extra_config=extra_config,
            )
            print_trace(db.run_id)
            print_event(
                "→",
                f"running simple_spine[{args.config}] "
                f"(model={cfg.main_model}, max_tokens={args.max_tokens})",
            )
            inputs = OrchInputs(
                question_id=full_id,
                budget=BudgetSpec(max_tokens=args.max_tokens),
            )
            result = await SimpleSpineOrchestrator(db, cfg).run(inputs)
            print_event(
                "✓",
                f"done: status={result.last_status}  tokens={result.tokens_used}  "
                f"spawns={result.spawn_count}  reason={result.finalize_reason}",
            )
            if result.structured_answer is not None:
                print(f"\nstructured_answer keys: {list(_keys(result.structured_answer))}")
            else:
                print(f"\nanswer ({len(result.answer_text)} chars):")
                print(truncate(result.answer_text, 400))
            return

        variant = settings.prioritizer_variant
        global_prio = settings.enable_global_prio
        print(f"workspace:    {ws}")
        print(f"question:     {full_id[:8]}  {truncate(page.headline, 80)}")
        print(f"orchestrator: {variant}{' (+global_prio)' if global_prio else ''}")

        await open_run(
            db,
            name=args.name or page.headline,
            question_id=full_id,
            skill="rumil-orchestrate",
            budget=args.budget,
            extra_config={"smoke_test": bool(args.smoke_test)},
        )
        print_trace(db.run_id)

        print_event("→", f"running {variant} orchestrator (budget {args.budget})")
        await Orchestrator(db).run(full_id)

        total, used = await db.get_budget()
        print_event("✓", f"done: budget={used}/{total}")
    finally:
        await db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
