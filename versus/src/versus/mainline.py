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

import re
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


_AXES_ORDER = (
    "prefix_config_hash",
    "judge_path",
    "judge_base_model",
    "judge_dimension",
    "judge_workspace_id",
    "judge_prompt_hash",
    "judge_sampling_hash",
    "judge_tool_hash",
    "judge_pair_hash",
    "judge_closer_hash",
    "judge_budget",
    "judge_code_fingerprint",
    "judge_workspace_state_hash",
    "config_hash",
)

_AXIS_DESCRIPTIONS = {
    "prefix_config_hash": (
        "Hash of essay text + prefix variant params (n_paragraphs, "
        "include_headers, length_tolerance) + COMPLETION_PROMPT_VERSION. "
        "Rows with the same prefix_config_hash were judged on the same "
        "essay slice."
    ),
    "judge_path": (
        "Which judge code path produced the row. blind = single LLM "
        "call; rumil:ws = SDK agent with workspace tools; rumil:orch = "
        "full orchestrator run; rumil:text = legacy."
    ),
    "judge_base_model": (
        "Underlying LLM model id (provider/<model> or just <model>) — "
        "post-decomposition, before any path/prompt/version/sampling "
        "suffixes."
    ),
    "judge_dimension": (
        "Criterion the judge was rendered for. Empty for blind judges "
        "that don't bake the dimension into their model id."
    ),
    "judge_workspace_id": (
        "First 8 chars of the rumil project (workspace) ID the ws/orch "
        "judge ran against. ws/orch only — empty on blind."
    ),
    "judge_prompt_hash": (
        "Hash of the rendered judge system prompt (shell + dimension "
        "body, with or without the workspace-tools section). Bumps "
        "when any of the source files change."
    ),
    "judge_sampling_hash": (
        "Hash of model sampling params (temperature, max_tokens, "
        "top_p). Lives on the row for blind/text variants."
    ),
    "judge_tool_hash": (
        "Hash of the {tool_name -> description} map for the workspace "
        "exploration tools. ws/orch only — empty on blind rows."
    ),
    "judge_pair_hash": (
        "Hash of the pair-surface page content (the Question page the "
        "ws/orch judge reads). Forks when the surface formatting "
        "changes."
    ),
    "judge_closer_hash": (
        "Hash of the orch closer config (user prompt template, "
        "max_turns, disallowed_tools, render detail/min_importance). "
        "orch only — bumps when any of those knobs change."
    ),
    "judge_budget": ("Orch run budget — bN means N total dispatches. orch only."),
    "judge_code_fingerprint": (
        "Collapsed sha8 over the bridge + orchestrator + calls + "
        "prompts + workspace-exploration content. ws/orch only — "
        "forks when any covered file's bytes change. Per-file detail "
        "lives in row['config']['code_fingerprint']."
    ),
    "judge_workspace_state_hash": (
        "Cheap watermark over baseline pages + page_links + mutation "
        "events at plan time. ws/orch only — two judgments with the "
        "same value saw the same baseline; forks when anything visible "
        "to the agent changes between runs."
    ),
    "config_hash": (
        "Composite sha16 over the full structured judge config. "
        "Acts as a completeness check on the per-axis projection: if "
        "a new config field gets added but its corresponding axis "
        "doesn't, this hash forks while every component axis still "
        "looks current. Rows with the same component axes but "
        "different ``config_hash`` values are the signal that the "
        "panel is missing a dimension."
    ),
}


def axis_descriptions() -> dict[str, str]:
    """Per-axis human description — what the hash is computed over,
    or what kind of value the axis carries."""
    return dict(_AXIS_DESCRIPTIONS)


def axis_order() -> tuple[str, ...]:
    return _AXES_ORDER


def current_values_summary(cfg: versus_config.Config) -> dict[str, list[str]]:
    """Mainline value set per provenance axis, for the UI to flag
    non-current rows.

    Axes derivable from cfg + version constants are filled here.
    ``prefix_config_hash`` is left empty because it depends on the
    set of essays the caller is aggregating over — the caller should
    union ``mainline.current_prefix_hashes_for`` across its essay
    set and merge the result in.
    """
    from rumil.versus_bridge import (
        compute_orch_closer_hash,
        compute_pair_surface_hash,
        compute_tool_prompt_hash,
    )
    from versus import judge as versus_judge

    out: dict[str, list[str]] = {axis: [] for axis in _AXES_ORDER}
    # Blind path also has both blind and tools-mode prompt hashes in
    # play (ws/orch use the tools-mode hash); union both so neither
    # judge family is mis-flagged as stale.
    out["judge_prompt_hash"] = [
        f"p{versus_judge.compute_judge_prompt_hash(c, with_tools=tools)}"
        for c in cfg.judging.criteria
        for tools in (False, True)
    ]
    out["judge_path"] = ["blind", "rumil:ws", "rumil:orch"]
    out["judge_base_model"] = list(cfg.judging.models)
    out["judge_dimension"] = list(cfg.judging.criteria)
    out["judge_tool_hash"] = [f"t{compute_tool_prompt_hash()}"]
    out["judge_pair_hash"] = [f"q{compute_pair_surface_hash()}"]
    out["judge_closer_hash"] = [f"c{compute_orch_closer_hash()}"]
    # Sampling hash is per-judge-path: blind uses _sampling_for which
    # depends on provider; ws/orch use _anthropic_sampling. Compute
    # the union over both so the panel doesn't false-flag either.
    sampling_hashes: set[str] = set()
    for m in cfg.judging.models:
        provider, canonical = versus_judge.route_judge_model(m)
        blind = versus_judge._sampling_for(provider, canonical, cfg.judging.max_tokens)
        sh = versus_judge.compute_sampling_hash(blind)
        if sh:
            sampling_hashes.add(f"s{sh}")
    out["judge_sampling_hash"] = sorted(sampling_hashes)
    return out


def summarize_provenance(rows: Iterable[dict]) -> dict[str, dict[str, int]]:
    """Per-axis ``value -> count`` over the rows.

    Reads from ``row["config"]`` (the structured judge config written
    by :func:`versus.judge_config.make_judge_config`). Pre-config rows
    were backfilled by ``versus/scripts/backfill_judge_config.py``;
    rows still without a config dict are skipped on the
    judge-side axes. New axes can be added to
    :func:`versus.judge_config.project_config_to_axes` without
    extending any parser.
    """
    from versus.judge_config import project_config_to_axes

    counts: dict[str, Counter] = {axis: Counter() for axis in _AXES_ORDER}

    for r in rows:
        ph = r.get("prefix_config_hash")
        if ph:
            counts["prefix_config_hash"][ph] += 1
        cfg = r.get("config")
        if isinstance(cfg, dict):
            for axis, value in project_config_to_axes(
                cfg, config_hash=r.get("config_hash")
            ).items():
                if axis in counts:
                    counts[axis][value] += 1
    return {axis: dict(c) for axis, c in counts.items()}
