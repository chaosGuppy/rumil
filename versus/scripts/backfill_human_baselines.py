"""Ensure a human held-out remainder row exists for every (active
essay × active prefix variant) combination.

The remainder is a pure function of the essay text + variant params,
so generating it costs nothing — but ``run_completions.py`` only
materializes it for whichever variant it was invoked under. Essays
seeded under just one variant end up with asymmetric coverage.

Run from any cwd; paths resolve relative to versus/.

  uv run --with-editable . versus/scripts/backfill_human_baselines.py
  uv run --with-editable . versus/scripts/backfill_human_baselines.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

VERSUS_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VERSUS_ROOT / "src"))

try:
    import rumil  # noqa: F401
except ModuleNotFoundError:
    sys.stderr.write(
        "[err] rumil isn't importable from this venv. Run from the rumil "
        "repo root, not versus/:\n"
        f"      cd {VERSUS_ROOT.parent} && uv run python versus/scripts/backfill_human_baselines.py ...\n"
    )
    raise SystemExit(1)

from versus import complete, config, jsonl, prepare  # noqa: E402
from versus import essay as versus_essay  # noqa: E402
from versus import mainline as versus_mainline  # noqa: E402


def _load_essays(cfg: config.Config) -> list[versus_essay.Essay]:
    cache_dir = cfg.essays.cache_dir
    if not cache_dir.is_absolute():
        cache_dir = VERSUS_ROOT / cache_dir
    exclude = set(cfg.essays.exclude_ids)
    essays: list[versus_essay.Essay] = []
    for path in sorted(cache_dir.glob("*.json")):
        if path.name.endswith(".verdict.json"):
            continue
        d = json.loads(path.read_text())
        if "source_id" not in d:
            continue
        if not versus_mainline.is_current_schema(d):
            continue
        if d["id"] in exclude:
            continue
        essays.append(
            versus_essay.Essay(
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
        )
    return essays


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=str(VERSUS_ROOT / "config.yaml"))
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be added without writing.",
    )
    args = ap.parse_args()

    cfg = config.load(args.config)
    log_path = cfg.storage.completions_log
    if not log_path.is_absolute():
        log_path = VERSUS_ROOT / log_path

    existing_keys: set[str] = set()
    if log_path.exists():
        for row in jsonl.read(log_path):
            k = row.get("key")
            if k:
                existing_keys.add(k)

    essays = _load_essays(cfg)
    variants = list(prepare.active_prefix_configs(cfg))
    print(f"essays: {len(essays)}  variants: {len(variants)}")

    added = 0
    skipped = 0
    for essay in essays:
        for v in variants:
            task = prepare.prepare(
                essay,
                n_paragraphs=v.n_paragraphs,
                include_headers=v.include_headers,
                length_tolerance=cfg.completion.length_tolerance,
            )
            k = complete.human_key(task.essay_id, task.prefix_config_hash)
            if k in existing_keys:
                skipped += 1
                continue
            if args.dry_run:
                print(f"  would add: {essay.id} / {v.id}  (hash={task.prefix_config_hash})")
            else:
                complete.ensure_human_baseline(task, log_path, existing_keys)
                print(f"  added: {essay.id} / {v.id}  (hash={task.prefix_config_hash})")
            added += 1

    verb = "would add" if args.dry_run else "added"
    print(f"\n{verb} {added} row(s) · already-current {skipped}")


if __name__ == "__main__":
    main()
