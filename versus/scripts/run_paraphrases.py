"""Generate model paraphrases for all cached essays."""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from versus import config, paraphrase, sources


def main() -> None:
    cfg = config.load("config.yaml")
    raw_html_dir = cfg.essays.cache_dir.parent / "raw_html"
    essays = sources.fetch_all(
        source_cfgs=cfg.essays.sources,
        cache_dir=cfg.essays.cache_dir,
        raw_html_dir=raw_html_dir,
    )
    paraphrase.run(cfg, essays)


if __name__ == "__main__":
    main()
