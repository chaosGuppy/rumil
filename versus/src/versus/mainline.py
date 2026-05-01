"""Provenance-axis registry for the versus results panel.

Declares the per-axis order + descriptions the FE renders, and two
helpers that aggregate over judgment rows: ``summarize_provenance``
counts axis values across rows, ``current_values_summary`` builds the
mainline value set from the active config so the panel can flag stale
or non-mainline rows.

The axis projection itself lives in
:func:`versus.judge_config.project_config_to_axes` — this module just
declares which axes the panel cares about (and in what order) plus
the human-readable descriptions.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable

from versus import config as versus_config

# Single source of truth: ordering matches insertion order; the panel
# reads ``AXES_ORDER`` directly. Add a new axis = add an entry here AND
# extend ``versus.judge_config.project_config_to_axes`` to populate it.
AXIS_DESCRIPTIONS: dict[str, str] = {
    "prefix_config_hash": (
        "Hash of essay text + prefix variant params (n_paragraphs, "
        "include_headers, length_tolerance) + COMPLETION_PROMPT_VERSION. "
        "Rows with the same prefix_config_hash were judged on the same "
        "essay slice."
    ),
    "judge_path": (
        "Which judge code path produced the row. blind = single LLM "
        "call; rumil:ws = SDK agent with workspace tools; rumil:orch = "
        "full orchestrator run."
    ),
    "judge_base_model": ("Underlying LLM model id (provider/<model> or just <model>)."),
    "judge_dimension": ("Criterion the judge was rendered for (e.g. general_quality, grounding)."),
    "judge_workspace_id": (
        "First 8 chars of the rumil project (workspace) ID the ws/orch "
        "judge ran against. ws/orch only — empty on blind."
    ),
    "judge_prompt_hash": (
        "Hash of the rendered judge system prompt (shell + dimension "
        "body, with or without the workspace-tools section). Bumps "
        "when any of the source files change."
    ),
    "judge_sampling_hash": ("Hash of model sampling params (temperature, max_tokens, top_p)."),
    "judge_tool_hash": (
        "Hash of the {tool_name -> description} map for the workspace "
        "exploration tools. ws/orch only."
    ),
    "judge_pair_hash": (
        "Hash of the pair-surface page content (the Question page the ws/orch judge reads)."
    ),
    "judge_closer_hash": (
        "Hash of the orch closer config (user prompt template, "
        "max_turns, disallowed_tools, render detail/min_importance). "
        "orch only."
    ),
    "judge_budget": "Orch run budget — bN means N total dispatches. orch only.",
    "judge_code_fingerprint": (
        "Collapsed sha8 over the bridge + orchestrator + calls + "
        "prompts + workspace-exploration content. ws/orch only — "
        "forks when any covered file's bytes change. Per-file detail "
        "lives in row['config']['code_fingerprint']."
    ),
    "judge_workspace_state_hash": (
        "Watermark over baseline pages + page_links visible to the "
        "agent at plan time. ws/orch only — two judgments with the "
        "same value saw the same baseline; forks when anything visible "
        "changes between runs."
    ),
    "config_hash": (
        "Composite sha16 over the full structured judge config. "
        "Acts as a completeness check on the per-axis projection: if "
        "a new config field gets added but its corresponding axis "
        "doesn't, this hash forks while every component axis still "
        "looks current."
    ),
}

AXES_ORDER: tuple[str, ...] = tuple(AXIS_DESCRIPTIONS)

# Axes that depend on per-run runtime state (workspace identity,
# essay set, current code fingerprint) — they don't have a single
# "mainline" value derivable from cfg alone, so
# ``current_values_summary`` skips them.
_RUNTIME_AXES: frozenset[str] = frozenset(
    {
        "prefix_config_hash",
        "judge_workspace_id",
        "judge_workspace_state_hash",
        "judge_code_fingerprint",
        "judge_budget",
        "config_hash",
    }
)


def current_values_summary(cfg: versus_config.Config) -> dict[str, list[str]]:
    """Mainline value set per provenance axis, for the UI to flag
    non-current rows.

    Builds a sample structured config for each (variant × model ×
    dimension) combination using current code, projects each through
    :func:`versus.judge_config.project_config_to_axes`, and unions the
    results per axis. Axes whose value depends on per-run runtime
    state (see ``_RUNTIME_AXES``) are returned empty — the caller
    layers in those values where it has them (e.g. the router unions
    ``prefix_config_hash`` across active essays).
    """
    from rumil.versus_bridge import (
        compute_orch_closer_hash,
        compute_pair_surface_hash,
        compute_tool_prompt_hash,
    )
    from versus import judge as versus_judge
    from versus.judge_config import make_judge_config, project_config_to_axes
    from versus.model_config import get_model_config

    out: dict[str, set[str]] = {axis: set() for axis in AXES_ORDER}
    thash = compute_tool_prompt_hash()
    qhash = compute_pair_surface_hash()
    chash = compute_orch_closer_hash()

    for criterion in cfg.judging.criteria:
        for model in cfg.judging.models:
            _, canonical = versus_judge.route_judge_model(model)
            mc = get_model_config(model, cfg=cfg)
            samples = []
            blind_cfg, _, _ = make_judge_config(
                "blind",
                model=canonical,
                dimension=criterion,
                model_config=mc,
                prompt_hash=versus_judge.compute_judge_prompt_hash(criterion, with_tools=False),
            )
            samples.append(blind_cfg)
            tools_ph = versus_judge.compute_judge_prompt_hash(criterion, with_tools=True)
            ws_cfg, _, _ = make_judge_config(
                "ws",
                model=canonical,
                dimension=criterion,
                model_config=mc,
                prompt_hash=tools_ph,
                tool_prompt_hash=thash,
                pair_surface_hash=qhash,
                workspace_id="<runtime>",
                code_fingerprint={"_": "_"},
                workspace_state_hash="<runtime>",
            )
            samples.append(ws_cfg)
            orch_cfg, _, _ = make_judge_config(
                "orch",
                model=canonical,
                dimension=criterion,
                model_config=mc,
                prompt_hash=tools_ph,
                tool_prompt_hash=thash,
                pair_surface_hash=qhash,
                workspace_id="<runtime>",
                code_fingerprint={"_": "_"},
                workspace_state_hash="<runtime>",
                budget=0,
                closer_hash=chash,
            )
            samples.append(orch_cfg)
            for c in samples:
                for axis, value in project_config_to_axes(c).items():
                    if axis in out and axis not in _RUNTIME_AXES:
                        out[axis].add(value)
    return {axis: sorted(values) for axis, values in out.items()}


def summarize_provenance(rows: Iterable[dict]) -> dict[str, dict[str, int]]:
    """Per-axis ``value -> count`` over the rows.

    Every row carries ``config`` + ``config_hash``; the projection
    routes them to the panel's axis counters. New axes are added via
    :func:`versus.judge_config.project_config_to_axes` and a matching
    entry in :data:`AXIS_DESCRIPTIONS`.
    """
    from versus.judge_config import project_config_to_axes

    counts: dict[str, Counter] = {axis: Counter() for axis in AXES_ORDER}

    for r in rows:
        ph = r.get("prefix_config_hash")
        if ph:
            counts["prefix_config_hash"][ph] += 1
        for axis, value in project_config_to_axes(
            r["config"], config_hash=r["config_hash"]
        ).items():
            if axis in counts:
                counts[axis][value] += 1
    return {axis: dict(c) for axis, c in counts.items()}
