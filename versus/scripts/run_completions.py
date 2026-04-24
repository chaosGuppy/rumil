"""Run completions for all cached essays.

Filters mirror run_judgments.py so targeted runs are possible without
editing config.yaml:
  --model <id>    (repeatable)  restrict to specific completion models
  --essay <id>    (repeatable)  restrict to specific essays
"""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from versus import complete, config, sources


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
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
    args = ap.parse_args()

    cfg = config.load("config.yaml")
    raw_html_dir = cfg.essays.cache_dir.parent / "raw_html"
    essays = sources.fetch_all(
        source_cfgs=cfg.essays.sources,
        cache_dir=cfg.essays.cache_dir,
        raw_html_dir=raw_html_dir,
    )

    if args.essay:
        keep = set(args.essay)
        essays = [e for e in essays if e.id in keep]
    if args.model:
        keep_models = set(args.model)
        cfg.completion.models = [m for m in cfg.completion.models if m.id in keep_models]
        if not cfg.completion.models:
            print(f"[err] no models matched --model {args.model}; check config.yaml")
            sys.exit(1)

    complete.run(cfg, essays)


if __name__ == "__main__":
    main()
