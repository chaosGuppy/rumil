"""Run pairwise judgments over all cached completions."""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from versus import config, judge  # noqa: E402


def main() -> None:
    cfg = config.load("config.yaml")
    judge.run(cfg)


if __name__ == "__main__":
    main()
