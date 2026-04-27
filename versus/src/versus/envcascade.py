"""Resolve API keys from a cascade of .env files plus the process environment.

Precedence (highest first): versus/.env, <rumil-root>/.env, process env. Files
are preferred over the process environment so per-project .env files override
stale shell exports. Only the keys explicitly requested are touched; nothing
else about os.environ is modified.

Intentionally no python-dotenv dependency — the parser handles KEY=VALUE lines
with surrounding quotes, which is sufficient for API keys.
"""

from __future__ import annotations

import os
import pathlib


def _read_env_file(path: pathlib.Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
            v = v[1:-1]
        if k:
            out[k] = v
    return out


def apply(
    keys: tuple[str, ...],
    *,
    versus_root: pathlib.Path,
    rumil_root: pathlib.Path,
) -> dict[str, str]:
    """Populate os.environ for `keys` using the cascade. Returns {key: source_label}.

    Source label is one of: 'versus/.env', '<rumil-root>/.env', 'env', 'missing'.
    Existing os.environ entries are overwritten only when a .env file supplied
    the value — the caller can see the resolution via the returned map.
    """
    versus_vals = _read_env_file(versus_root / ".env")
    rumil_vals = _read_env_file(rumil_root / ".env")
    sources: dict[str, str] = {}
    for key in keys:
        if key in versus_vals:
            os.environ[key] = versus_vals[key]
            sources[key] = "versus/.env"
        elif key in rumil_vals:
            os.environ[key] = rumil_vals[key]
            sources[key] = f"{rumil_root.name}/.env"
        elif key in os.environ:
            sources[key] = "env"
        else:
            sources[key] = "missing"
    return sources
