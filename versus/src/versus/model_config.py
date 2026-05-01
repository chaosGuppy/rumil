"""Versus per-model registry — the operator's lever for what gets sent.

Maps a model id (as it appears in CLI / config) to the effective
``rumil.model_config.ModelConfig`` versus applies on the wire. Both
direct paths (completions, paraphrases, blind judge) and bridge paths
(ws/orch) read from here, so a single yaml edit changes the effective
condition everywhere consistently. Every row records its model_config
snapshot, so the dedup hash forks naturally on any change.

Why versus has its own registry instead of leaning on
``rumil.llm.thinking_config`` / ``effort_level``: those are rumil's
defaults for non-versus callers. Versus wants explicit control so the
operator can tune (e.g.) "haiku with thinking on" without touching
rumil's main path.
"""

from __future__ import annotations

from rumil.model_config import ModelConfig
from versus import config as versus_config


def get_model_config(model_id: str, *, cfg: versus_config.Config | None = None) -> ModelConfig:
    """Resolve the effective ``ModelConfig`` for ``model_id``.

    Reads from ``cfg.models[model_id]`` and translates into the
    rumil-side ``ModelConfig`` shape. Raises ``KeyError`` with a
    pointer to the registry on miss; the Config validator catches
    typos at load time, so a runtime miss should be rare.
    """
    if cfg is None:
        cfg = versus_config.load("config.yaml")
    entry = cfg.models.get(model_id)
    if entry is None:
        raise KeyError(
            f"model {model_id!r} not in versus models registry — add an entry to config.yaml"
        )
    return ModelConfig(
        temperature=entry.sampling.temperature,
        max_tokens=entry.sampling.max_tokens,
        top_p=entry.sampling.top_p,
        thinking=dict(entry.thinking) if entry.thinking is not None else None,
        effort=entry.effort,
        max_thinking_tokens=entry.max_thinking_tokens,
        service_tier=entry.service_tier,
    )


def get_judge_model_config(
    model_id: str, *, cfg: versus_config.Config | None = None
) -> ModelConfig:
    """``get_model_config`` with the judge-purpose max_tokens override applied.

    Judges typically need more output headroom than completions on the
    same model (reasoning judges chew through tokens). The registry
    holds a sane completion-purpose default; this layer replaces
    ``max_tokens`` with ``cfg.judging.max_tokens`` when set, leaving
    everything else (temperature, thinking, effort, etc.) intact.
    """
    if cfg is None:
        cfg = versus_config.load("config.yaml")
    mc = get_model_config(model_id, cfg=cfg)
    override = cfg.judging.max_tokens
    if override is not None and override != mc.max_tokens:
        from dataclasses import replace

        mc = replace(mc, max_tokens=override)
    return mc
