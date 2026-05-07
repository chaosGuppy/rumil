"""Registry mapping YAML ``orch_factory_key`` strings to runtime factories.

A :class:`NestedOrchSubroutine` wraps a ``NestedOrchFactory`` —
``Callable[[SpawnCtx, int, Mapping[str, Any]], Awaitable[str]]`` —
that takes the spawn context, a carved sub-token-cap, and the spawning
agent's overrides, and returns a coroutine yielding a text summary
that bubbles back to mainline.

Two built-ins land here:

- ``simple_spine_recurse`` — recurse into a named SimpleSpine preset
  with a carved sub-clock. Mainline can spawn an entire sub-orch with
  its own subroutine library and finalize back to mainline as a single
  text result. Overrides: ``preset_name`` (which preset to recurse
  into), ``token_cap`` (override the static base cap).
- ``simple_spine_self`` — recurse into the *currently-running* preset.
  Useful for "investigate this sub-question with the same orch shape"
  patterns. The sub-orch's library is the parent's library.

Add more via :func:`register_orch_factory`. Built-ins register lazily
so heavy imports don't land at module load time.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from rumil.orchestrators.simple_spine.subroutines.base import SpawnCtx
from rumil.orchestrators.simple_spine.subroutines.nested_orch import NestedOrchFactory

_REGISTRY: dict[str, NestedOrchFactory] = {}
_BUILTINS_LOADED = False


def register_orch_factory(key: str, factory: NestedOrchFactory) -> None:
    """Register a NestedOrchFactory under ``key`` for YAML lookup."""
    _REGISTRY[key] = factory


def get_orch_factory(key: str) -> NestedOrchFactory:
    if not _BUILTINS_LOADED:
        _register_builtins()
    if key not in _REGISTRY:
        known = sorted(_REGISTRY)
        raise KeyError(f"unknown orch_factory_key {key!r}; registered: {known}")
    return _REGISTRY[key]


def list_orch_factories() -> list[str]:
    if not _BUILTINS_LOADED:
        _register_builtins()
    return sorted(_REGISTRY)


async def _simple_spine_recurse(
    ctx: SpawnCtx, sub_token_cap: int, overrides: Mapping[str, Any]
) -> str:
    """Recurse into a named SimpleSpine preset under a carved sub-clock.

    Required override: ``preset_name`` (string registry key for the
    nested preset). Optional: ``additional_context``, ``intent``.
    The nested orch's deliverable text is returned to mainline verbatim.
    """
    from rumil.models import CallType
    from rumil.orchestrators.simple_spine.config import OrchInputs
    from rumil.orchestrators.simple_spine.orchestrator import SimpleSpineOrchestrator
    from rumil.orchestrators.simple_spine.presets import get_preset

    preset_name = str(overrides.get("preset_name", "")) or "default"
    sub_cfg = get_preset(preset_name)
    sub_clock = ctx.budget_clock.carve_child(sub_token_cap)
    sub_inputs = OrchInputs(
        question_id=ctx.question_id,
        additional_context=str(overrides.get("additional_context", "")),
        operating_assumptions="",
        output_guidance=str(overrides.get("intent", "")),
        budget=sub_clock.spec,
    )
    sub_orch = SimpleSpineOrchestrator(ctx.db, sub_cfg, broadcaster=ctx.broadcaster)
    result = await sub_orch.run(
        sub_inputs,
        call_type=CallType.CLAUDE_CODE_DIRECT,
        parent_call_id=ctx.parent_call_id,
        budget_clock=sub_clock,
    )
    return result.answer_text


async def _simple_spine_self(
    ctx: SpawnCtx, sub_token_cap: int, overrides: Mapping[str, Any]
) -> str:
    """Recurse into the same preset that the parent orch is running.

    Equivalent to ``simple_spine_recurse`` with ``preset_name`` set to
    whatever the parent was using, but the parent's preset name isn't
    available on the SpawnCtx. This factory expects the YAML config to
    pass ``preset_name`` explicitly via the SubroutineDef's static
    config; if absent, falls back to ``default``.
    """
    return await _simple_spine_recurse(ctx, sub_token_cap, overrides)


def _register_builtins() -> None:
    global _BUILTINS_LOADED
    register_orch_factory("simple_spine_recurse", _simple_spine_recurse)
    register_orch_factory("simple_spine_self", _simple_spine_self)
    _BUILTINS_LOADED = True
