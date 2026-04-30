"""Run completions for the canonical active essay set.

Default scope is the same gate ``/versus`` applies: current
``schema_version`` and not in ``cfg.essays.exclude_ids``. Pass
``--include-stale`` to instead run over every essay returned by the
source fetchers (minus ``exclude_ids``) — useful for backfill or
debugging old-schema rows.

Filters mirror run_judgments.py so targeted runs are possible without
editing config.yaml:
  --model <id>    (repeatable)  restrict to specific completion models
  --essay <id>    (repeatable)  restrict to specific essays
  --include-stale               run over all fetched essays, not just the active set
  --prefix-label <id>           target a specific prefix variant (default: canonical)
"""

from __future__ import annotations

import argparse
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
    for prefix_cfg in prefix_cfgs:
        print(f"[prefix] using variant {prefix_cfg.id!r} (db={target})")
        complete.run(cfg, essays, prefix_cfg=prefix_cfg, prod=args.prod)


if __name__ == "__main__":
    main()
