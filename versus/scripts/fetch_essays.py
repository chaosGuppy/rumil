"""Fetch recent forethought.org essays per config."""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from versus import config, fetch  # noqa: E402


def main() -> None:
    cfg = config.load("config.yaml")
    raw_html_dir = cfg.essays.cache_dir.parent / "raw_html"
    essays = fetch.fetch(
        cache_dir=cfg.essays.cache_dir,
        raw_html_dir=raw_html_dir,
        max_recent=cfg.essays.max_recent,
    )
    print(f"fetched {len(essays)} essays:")
    for e in essays:
        types = {}
        for b in e.blocks:
            types[b.type] = types.get(b.type, 0) + 1
        print(f"  - {e.id}: {e.title!r} ({types})")


if __name__ == "__main__":
    main()
