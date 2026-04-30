"""Per-source essay fetchers.

Each source module in this package exports a ``fetch(source_cfg, cache_dir,
raw_html_dir, client=None) -> list[Essay]`` function. The ``SOURCES``
registry maps source ids (as used in ``config.yaml``) to their fetcher.
"""

from __future__ import annotations

import pathlib
from collections.abc import Callable

import httpx

from versus.essay import Essay
from versus.sources import carlsmith, forethought, redwood

Fetcher = Callable[..., list[Essay]]

SOURCES: dict[str, Fetcher] = {
    "forethought": forethought.fetch,
    "redwood": redwood.fetch,
    "carlsmith": carlsmith.fetch,
}


def fetch_all(
    source_cfgs,
    cache_dir: pathlib.Path,
    raw_html_dir: pathlib.Path,
    client: httpx.Client | None = None,
    *,
    prod: bool = False,
) -> list[Essay]:
    """Iterate configured sources in declaration order and concatenate results.

    Opens a shared ``httpx.Client`` if one isn't provided so the per-source
    fetchers reuse connections.
    """
    close = client is None
    client = client or httpx.Client(
        follow_redirects=True,
        headers={"User-Agent": "versus-eval/0.0.1"},
        timeout=30.0,
    )
    try:
        essays: list[Essay] = []
        for sc in source_cfgs:
            fetcher = SOURCES.get(sc.id)
            if fetcher is None:
                raise ValueError(f"unknown essay source {sc.id!r}; available: {sorted(SOURCES)}")
            essays.extend(
                fetcher(
                    source_cfg=sc,
                    cache_dir=cache_dir,
                    raw_html_dir=raw_html_dir,
                    client=client,
                    prod=prod,
                )
            )
        return essays
    finally:
        if close:
            client.close()
