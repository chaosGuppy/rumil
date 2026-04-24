"""Tiny JSONL append-only store with dedup-by-key support.

Reads are cached keyed on ``(path, mtime_ns, size)``. The /versus/results
endpoint scans a single judgments_log several times per request (for the
matrix aggregate, the content-test aggregate, and the raw rows list) —
without caching that's a real IO+parse tax on every page load. The cache
is invalidated automatically whenever the file's mtime or size changes,
and explicitly by ``append()``.

Contract: consumers must treat yielded dicts as read-only. The cache
shares the parsed list across calls, so mutating a row in place would
silently corrupt later reads within the same process.
"""

from __future__ import annotations

import json
import pathlib
from collections.abc import Iterator


# {abs_path_str: (mtime_ns, size, parsed_rows)}
_READ_CACHE: dict[str, tuple[int, int, list[dict]]] = {}


def _cached_rows(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    stat = path.stat()
    mtime = stat.st_mtime_ns
    size = stat.st_size
    key = str(path.resolve())
    cached = _READ_CACHE.get(key)
    if cached is not None and cached[0] == mtime and cached[1] == size:
        return cached[2]
    rows: list[dict] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    _READ_CACHE[key] = (mtime, size, rows)
    return rows


def read(path: pathlib.Path) -> Iterator[dict]:
    yield from _cached_rows(path)


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
    # mtime+size usually changes on append, which would invalidate the cache
    # naturally. Drop the entry explicitly to guarantee the next read sees
    # the new row even if the filesystem's mtime resolution is coarse.
    _READ_CACHE.pop(str(path.resolve()), None)
