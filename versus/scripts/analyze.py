"""Print the gen-model × judge-model matrix of %-picks-human."""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from versus import analyze, config


def main() -> None:
    cfg = config.load("config.yaml")
    data = analyze.matrix(cfg.storage.judgments_log)
    if not data:
        print("(no judgments yet)")
        return

    conditions = sorted({k[2] for k in data})
    criteria = sorted({k[3] for k in data})

    for cond in conditions:
        print()
        print("=" * 78)
        print(analyze.format_matrix(data, condition=cond))
        for crit in criteria:
            print()
            print(analyze.format_matrix(data, condition=cond, criterion=crit))


if __name__ == "__main__":
    main()
