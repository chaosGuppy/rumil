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


def read_dedup(path: pathlib.Path, key_field: str = "key") -> Iterator[dict]:
    """Yield rows with duplicate `key_field` collapsed, last-row-wins.

    Append-only writes don't enforce uniqueness, and parallel runners can
    race past `keys()` and each append a row with the same dedup key.
    Read paths that aggregate (matrix counts) or render lists (UI tables)
    should use this so a duplicate doesn't double-count or break React keys.
    Rows missing the key field are passed through unchanged.
    """
    by_key: dict[str, dict] = {}
    keyless: list[dict] = []
    for row in read(path):
        k = row.get(key_field)
        if k is None:
            keyless.append(row)
        else:
            by_key[k] = row
    yield from keyless
    yield from by_key.values()


def append(path: pathlib.Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
