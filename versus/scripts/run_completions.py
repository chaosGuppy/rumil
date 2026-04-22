"""Run completions for all cached essays."""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from versus import complete, config, fetch  # noqa: E402


def main() -> None:
    cfg = config.load("config.yaml")
    raw_html_dir = cfg.essays.cache_dir.parent / "raw_html"
    essays = fetch.fetch(
        cache_dir=cfg.essays.cache_dir,
        raw_html_dir=raw_html_dir,
        max_recent=cfg.essays.max_recent,
    )
    complete.run(cfg, essays)


if __name__ == "__main__":
    main()
