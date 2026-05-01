"""Run completions for the canonical active essay set.

Default scope is the same gate ``/versus`` applies: current
``schema_version`` and not in ``cfg.essays.exclude_ids``. Pass
``--include-stale`` to instead run over every essay returned by the
source fetchers (minus ``exclude_ids``) — useful for backfill or
debugging old-schema rows.

Two paths share this script:

- **Single-shot** (default): one LLM call per essay × prefix × model.
  Behaviour preserved byte-for-byte from before the orch path landed —
  the existing ``request_hash`` covers all effective inputs.
- **Orch** (``--orch <workflow>``): runs a rumil workflow (TwoPhase,
  later DraftAndEdit) against a per-essay Question, then a closing
  call extracts a finished continuation. Lands as a ``versus_texts``
  row tagged ``source_id="orch:<workflow>:<model>:c<hash8>"`` so
  judges can pair orch outputs against single-shot or human baselines.

Filters mirror run_judgments.py so targeted runs are possible without
editing config.yaml:
  --model <id>    (repeatable)  restrict to specific completion models
  --essay <id>    (repeatable)  restrict to specific essays
  --include-stale               run over all fetched essays, not just the active set
  --prefix-label <id>           target a specific prefix variant (default: canonical)
  --orch <workflow_name>        switch to the orch path (requires --workspace, --budget)
  --workspace <name>            rumil workspace (orch only; required)
  --budget N                    orch budget per essay (orch only)
  --persist                     orch only — disable staging
  --concurrency N               orch only — concurrent runs
  --limit N                     cap planned essays (orch only; honored before firing)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys

VERSUS_ROOT = pathlib.Path(__file__).resolve().parent.parent
RUMIL_ROOT = VERSUS_ROOT.parent

sys.path.insert(0, str(VERSUS_ROOT / "src"))

# versus's pyproject can't depend on rumil (would be circular), so a
# script run from versus/ cwd hits a deep ModuleNotFoundError when
# transitively importing rumil. Catch that early and point at the fix.
try:
    import rumil  # noqa: F401
except ModuleNotFoundError:
    sys.stderr.write(
        "[err] rumil isn't importable from this venv. Run from the rumil "
        "repo root, not versus/:\n"
        f"      cd {RUMIL_ROOT} && uv run python versus/scripts/run_completions.py ...\n"
    )
    raise SystemExit(1) from None

from versus import complete, config, envcascade, prepare, sources, versus_db  # noqa: E402
from versus import essay as versus_essay  # noqa: E402

envcascade.apply(
    ("ANTHROPIC_API_KEY", "OPENROUTER_API_KEY"),
    versus_root=VERSUS_ROOT,
    rumil_root=RUMIL_ROOT,
)


def _load_essay_from_cache(cache_dir: pathlib.Path, essay_id: str) -> versus_essay.Essay | None:
    """Load a cached Essay by id, bypassing the source fetcher.

    Used when --essay names an essay no longer in the source's live
    recent feed (e.g. forethought rolled an older post off its top-N).
    Returns None if the essay isn't cached, lacks ``source_id`` (legacy
    pre-multi-source JSON), or doesn't match the current schema_version.
    """
    p = cache_dir / f"{essay_id}.json"
    if not p.is_file():
        return None
    d = json.loads(p.read_text())
    if "source_id" not in d:
        return None
    if not versus_essay.is_current_schema(d):
        return None
    return versus_essay.Essay(
        id=d["id"],
        source_id=d["source_id"],
        url=d.get("url", ""),
        title=d.get("title", ""),
        author=d.get("author", ""),
        pub_date=d.get("pub_date", ""),
        blocks=[versus_essay.Block(**b) for b in d["blocks"]],
        markdown=d.get("markdown", ""),
        image_count=d.get("image_count", 0),
        schema_version=d.get("schema_version", 0),
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=str(VERSUS_ROOT / "config.yaml"))
    ap.add_argument(
        "--model",
        action="append",
        default=None,
        help="OpenRouter completion model id, repeatable. Overrides completion.models.",
    )
    ap.add_argument(
        "--essay",
        action="append",
        default=None,
        help="Restrict to specified essay_id(s). Repeatable.",
    )
    ap.add_argument(
        "--include-stale",
        action="store_true",
        help=(
            "Default behavior is the canonical active set (current "
            "schema_version, not in cfg.essays.exclude_ids — same gate "
            "/versus applies). Pass this to run over every fetched essay "
            "instead, including off-feed or old-schema rows. "
            "exclude_ids is still honored."
        ),
    )
    ap.add_argument(
        "--prefix-label",
        action="append",
        default=None,
        help=(
            "Run completions under a specific prefix variant. Repeatable: "
            "pass once per variant to run multiple in one invocation. "
            "Default: the canonical `prefix:` entry. Sibling variants are "
            "listed under `prefix_variants:` in config.yaml; pass their `id`."
        ),
    )
    ap.add_argument(
        "--prod",
        action="store_true",
        help="Target the production Supabase database (default: local).",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the planned completion calls and exit without firing API requests.",
    )
    ap.add_argument(
        "--orch",
        default=None,
        help=(
            "Switch to orch-driven completions: run the named workflow "
            "(e.g. 'two_phase') against a per-essay Question, then a closing "
            "call emits a continuation. Requires --workspace and --budget. "
            "When omitted, falls through to the single-shot completion path. "
            "Output rows are tagged source_id=orch:<workflow>:<model>:c<hash8>."
        ),
    )
    ap.add_argument(
        "--workspace",
        default=None,
        help="Rumil workspace (project) name. Required when --orch is set; ignored otherwise.",
    )
    ap.add_argument(
        "--budget",
        type=int,
        default=4,
        help=(
            "Orch only: research-call budget per essay. TwoPhaseOrchestrator "
            "requires a minimum of 4. Default: 4."
        ),
    )
    ap.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help="Orch only: concurrent runs. Default: 1 (serial).",
    )
    ap.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Orch only: cap on number of essays processed.",
    )
    ap.add_argument(
        "--persist",
        action="store_true",
        help=(
            "Orch only: persist versus-created pages to baseline instead "
            "of staging them. Default is staged: the agent still reads "
            "baseline workspace material but its Question pages and the "
            "orch research subtree are scoped to the run's staged view "
            "— invisible to other readers of the workspace. The final "
            "completion text always lands in versus_texts regardless."
        ),
    )
    args = ap.parse_args()

    cfg = config.load(args.config)
    prefix_cfgs = (
        [prepare.resolve_prefix_cfg(cfg, label) for label in args.prefix_label]
        if args.prefix_label
        else [prepare.resolve_prefix_cfg(cfg, None)]
    )
    if not cfg.essays.cache_dir.is_absolute():
        cfg.essays.cache_dir = VERSUS_ROOT / cfg.essays.cache_dir
    if not cfg.storage.paraphrases_log.is_absolute():
        cfg.storage.paraphrases_log = VERSUS_ROOT / cfg.storage.paraphrases_log

    raw_html_dir = cfg.essays.cache_dir.parent / "raw_html"
    essays = sources.fetch_all(
        source_cfgs=cfg.essays.sources,
        cache_dir=cfg.essays.cache_dir,
        raw_html_dir=raw_html_dir,
        prod=args.prod,
    )

    exclude = set(cfg.essays.exclude_ids)
    essays = [e for e in essays if e.id not in exclude]

    if not args.include_stale:
        # Default: restrict to the canonical eval set. This is a hard
        # list, not an intersection with fetch_all — active essays that
        # have rolled off the source's live feed but are still valid in
        # cache get loaded directly so the run honors --active in full.
        active = prepare.active_essay_ids(
            cfg.essays.exclude_ids, client=versus_db.get_client(prod=args.prod)
        )
        fetched_ids = {e.id for e in essays}
        for missing_id in sorted(active - fetched_ids):
            cached = _load_essay_from_cache(cfg.essays.cache_dir, missing_id)
            if cached is None:
                print(f"[warn] active {missing_id}: not in cache or schema mismatch; skipping")
                continue
            print(f"[essay] {missing_id}: loaded from cache (off live feed)")
            essays.append(cached)
        essays = [e for e in essays if e.id in active]
    if args.essay:
        # --essay is a hard list, not an intersection with fetch_all.
        # An essay can be cached + valid but absent from the source's
        # live recent feed (e.g. rolled off a top-N index); load those
        # straight from cache so the run honors the user's request.
        keep = set(args.essay)
        fetched_ids = {e.id for e in essays}
        for missing_id in sorted(keep - fetched_ids):
            cached = _load_essay_from_cache(cfg.essays.cache_dir, missing_id)
            if cached is None:
                print(f"[warn] --essay {missing_id}: not in cache or schema mismatch; skipping")
                continue
            print(f"[essay] {missing_id}: loaded from cache (off live feed)")
            essays.append(cached)
        essays = [e for e in essays if e.id in keep]
    if args.model:
        keep_models = set(args.model)
        cfg.completion.models = [m for m in cfg.completion.models if m.id in keep_models]
        if not cfg.completion.models:
            print(f"[err] no models matched --model {args.model}; check config.yaml")
            sys.exit(1)

    target = "prod" if args.prod else "local"

    if args.orch is not None:
        # Orch path: dispatch to the rumil_completion driver. Single-shot
        # path is bypassed entirely so the existing request_hash / row
        # shape is unaffected.
        if not args.workspace:
            ap.error("--orch requires --workspace <name>")
        if not args.model:
            ap.error("--orch requires --model <id> (single model per run)")
        if len(args.model) > 1:
            ap.error(f"--orch takes at most one --model (got {len(args.model)}: {args.model})")
        # Resolve through rumil's alias table so 'opus' / 'sonnet' / 'haiku'
        # work the same way they do for run_rumil_judgments.py.
        from rumil.settings import resolve_model_alias
        from versus import rumil_completion

        model_id = resolve_model_alias(args.model[0])
        for prefix_cfg in prefix_cfgs:
            print(f"[prefix] using variant {prefix_cfg.id!r} (db={target}, orch={args.orch!r})")
            asyncio.run(
                rumil_completion.run_orch_completion(
                    cfg,
                    essays,
                    workspace=args.workspace,
                    workflow_name=args.orch,
                    model=model_id,
                    budget=args.budget,
                    prefix_cfg=prefix_cfg,
                    limit=args.limit,
                    dry_run=args.dry_run,
                    concurrency=args.concurrency,
                    persist=args.persist,
                    prod=args.prod,
                )
            )
        return

    for prefix_cfg in prefix_cfgs:
        print(f"[prefix] using variant {prefix_cfg.id!r} (db={target})")
        complete.run(cfg, essays, prefix_cfg=prefix_cfg, prod=args.prod, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
