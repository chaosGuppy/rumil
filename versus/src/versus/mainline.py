"""Single-source helpers for "what's currently mainline."

The yaml config + the per-module version constants
(``SCHEMA_VERSION``, ``BLIND_JUDGE_VERSION``, etc) together define
which slice of the data is "current and good." This module exposes
small helpers so list endpoints, status reports, and aggregate
endpoints all answer the question the same way.

No new abstraction — the helpers just project (config + constants)
onto the axes callers care about.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable

from versus import config as versus_config
from versus import essay as versus_essay


def is_current_schema(d: dict) -> bool:
    """True if a cached essay JSON's schema matches the live version."""
    return d.get("schema_version", 0) == versus_essay.SCHEMA_VERSION


def current_prefix_hashes_for(essay: versus_essay.Essay, cfg: versus_config.Config) -> set[str]:
    """All ``prefix_config_hash`` values that are currently in play for
    this essay, across every active prefix variant in the config."""
    from versus import prepare as versus_prepare

    out: set[str] = set()
    for p in versus_prepare.active_prefix_configs(cfg):
        task = versus_prepare.prepare(
            essay,
            n_paragraphs=p.n_paragraphs,
            include_headers=p.include_headers,
            length_tolerance=cfg.completion.length_tolerance,
        )
        out.add(task.prefix_config_hash)
    return out


def current_values_summary(cfg: versus_config.Config) -> dict[str, list[str]]:
    """Mainline value set per provenance axis, for the UI to flag
    non-current rows.

    Axes derivable from cfg + version constants are filled here.
    ``prefix_config_hash`` is left empty because it depends on the
    set of essays the caller is aggregating over — the caller should
    union ``mainline.current_prefix_hashes_for`` across its essay
    set and merge the result in.
    """
    from versus import judge as versus_judge
    from versus.versions import BLIND_JUDGE_VERSION

    return {
        "prefix_config_hash": [],
        "judge_model": [],
        "judge_prompt_hash": [
            f"p{versus_judge.compute_judge_prompt_hash(c, with_tools=False)}"
            for c in cfg.judging.criteria
        ],
        "judge_version": [f"v{BLIND_JUDGE_VERSION}"],
        "sampling_hash": [],
    }


def summarize_provenance(rows: Iterable[dict]) -> dict[str, dict[str, int]]:
    """Per-axis ``value -> count`` over the rows.

    Axes covered: ``prefix_config_hash``, ``judge_model``,
    ``judge_prompt_hash`` (parsed from judge_model), ``judge_version``,
    ``sampling_hash``. Empty/missing values are skipped per-axis. The
    UI renders this as the "what slice did we aggregate over?" panel.
    """
    counts: dict[str, Counter] = {
        "prefix_config_hash": Counter(),
        "judge_model": Counter(),
        "judge_prompt_hash": Counter(),
        "judge_version": Counter(),
        "sampling_hash": Counter(),
    }
    from versus import judge as versus_judge

    for r in rows:
        ph = r.get("prefix_config_hash")
        if ph:
            counts["prefix_config_hash"][ph] += 1
        jm = r.get("judge_model")
        if jm:
            counts["judge_model"][jm] += 1
            base, prompt_hash, version = versus_judge.parse_judge_model_suffix(jm)
            if prompt_hash:
                counts["judge_prompt_hash"][prompt_hash] += 1
            if version:
                counts["judge_version"][version] += 1
        sh = r.get("sampling_hash")
        if sh:
            counts["sampling_hash"][sh] += 1
    return {axis: dict(c) for axis, c in counts.items()}
