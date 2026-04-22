"""Tiny JSONL append-only store with dedup-by-key support."""

from __future__ import annotations

import json
import pathlib
from collections.abc import Iterator


def read(path: pathlib.Path) -> Iterator[dict]:
    if not path.exists():
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def keys(path: pathlib.Path, key_field: str = "key") -> set[str]:
    return {row[key_field] for row in read(path) if key_field in row}


def append(path: pathlib.Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
