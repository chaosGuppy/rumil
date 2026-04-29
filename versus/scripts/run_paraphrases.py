"""Generate model paraphrases for all cached essays.

  --essay <id>    (repeatable)  restrict to specific essays
  --active                      canonical eval set only (current schema, not excluded)

``cfg.essays.exclude_ids`` is always honored, so excluded essays never
get paraphrases even without ``--active``. Run from any cwd — paths
resolve relative to versus/.
"""

from __future__ import annotations

import argparse
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
        f"      cd {VERSUS_ROOT.parent} && uv run python versus/scripts/run_paraphrases.py ...\n"
    )
    raise SystemExit(1)

from versus import config, paraphrase, prepare, sources  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=str(VERSUS_ROOT / "config.yaml"))
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
            "Restrict to the canonical active set: current schema_version and "
            "not in cfg.essays.exclude_ids. Same gate /versus applies."
        ),
    )
    args = ap.parse_args()

    cfg = config.load(args.config)
    if not cfg.essays.cache_dir.is_absolute():
        cfg.essays.cache_dir = VERSUS_ROOT / cfg.essays.cache_dir
    if not cfg.storage.paraphrases_log.is_absolute():
        cfg.storage.paraphrases_log = VERSUS_ROOT / cfg.storage.paraphrases_log

    raw_html_dir = cfg.essays.cache_dir.parent / "raw_html"
    essays = sources.fetch_all(
        source_cfgs=cfg.essays.sources,
        cache_dir=cfg.essays.cache_dir,
        raw_html_dir=raw_html_dir,
    )

    exclude = set(cfg.essays.exclude_ids)
    essays = [e for e in essays if e.id not in exclude]

    if args.active:
        active = prepare.active_essay_ids(cfg.essays.cache_dir, cfg.essays.exclude_ids)
        essays = [e for e in essays if e.id in active]
    if args.essay:
        keep = set(args.essay)
        essays = [e for e in essays if e.id in keep]

    paraphrase.run(cfg, essays)


if __name__ == "__main__":
    main()
