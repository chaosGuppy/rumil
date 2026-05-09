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
  text result. The factory creates a NEW child Question linked under
  the parent's scope question and runs the nested orch against it; the
  new page inherits the parent run's staging via ``ctx.db``.
- ``simple_spine_self`` — recurse into the *currently-running* preset.
  Useful for "investigate this sub-question with the same orch shape"
  patterns. The sub-orch's library is the parent's library.

Add more via :func:`register_orch_factory`. Built-ins register lazily
so heavy imports don't land at module load time.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from rumil.models import LinkType, PageLayer, PageType, Workspace
from rumil.orchestrators.simple_spine.subroutines.base import SpawnCtx
from rumil.orchestrators.simple_spine.subroutines.nested_orch import NestedOrchFactory

log = logging.getLogger(__name__)

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

    Creates a NEW child Question (headline from ``question_headline``
    override; optional ``question_content``) linked as ``CHILD_QUESTION``
    of the parent's scope question, and runs the nested orch against
    that new question. The new page inherits the parent run's staging
    flags via ``ctx.db`` (``staged`` / ``run_id`` are set on the DB
    instance), so a staged parent's child questions are also staged.

    Overrides:
    - ``question_headline`` (required): headline for the new child
    - ``question_content`` (optional): clarifying content
    - ``intent`` (required by base schema): freeform investigative steer
    - ``output_guidance``, ``output_schema``: shape the deliverable
    - ``additional_context``: extra framing for the child orch
    - ``token_cap``: override the static base cap
    - ``preset_name``: which preset to recurse into (defaults to
      ``default``)

    Returns the nested orch's deliverable text verbatim.
    """
    from rumil.embeddings import embed_and_store_page
    from rumil.models import CallType, Page, PageLink
    from rumil.orchestrators.simple_spine.config import OrchInputs
    from rumil.orchestrators.simple_spine.orchestrator import SimpleSpineOrchestrator
    from rumil.orchestrators.simple_spine.presets import get_preset

    headline = str(overrides.get("question_headline") or "").strip()
    if not headline:
        raise ValueError(
            "nested_orch override `question_headline` is required — recurse "
            "creates a new child question and needs its headline"
        )
    raw_schema = overrides.get("output_schema")
    if raw_schema is not None and not isinstance(raw_schema, dict):
        raise ValueError(
            f"nested_orch override `output_schema` must be a JSON Schema dict, "
            f"got {type(raw_schema).__name__}"
        )

    parent_question = await ctx.db.get_page(ctx.question_id)
    if parent_question is None:
        raise ValueError(
            f"nested_orch: parent question {ctx.question_id} not found — cannot "
            f"create a child question under a non-existent parent"
        )
    parent_call = await ctx.db.get_call(ctx.parent_call_id)
    provenance_call_type = parent_call.call_type.value if parent_call else ""

    child_question = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=parent_question.workspace or Workspace.RESEARCH,
        content=str(overrides.get("question_content") or ""),
        headline=headline,
        provenance_call_type=provenance_call_type,
        provenance_call_id=ctx.parent_call_id,
    )
    await ctx.db.save_page(child_question)
    await ctx.db.save_link(
        PageLink(
            from_page_id=ctx.question_id,
            to_page_id=child_question.id,
            link_type=LinkType.CHILD_QUESTION,
            reasoning="Spawned via simple_spine_recurse",
        )
    )
    try:
        await embed_and_store_page(ctx.db, child_question, field_name="abstract")
    except Exception:
        log.warning(
            "simple_spine_recurse: embedding failed for child question %s",
            child_question.id[:8],
            exc_info=True,
        )
    log.info(
        "simple_spine_recurse: created child question %s under parent %s",
        child_question.id[:8],
        ctx.question_id[:8],
    )

    from rumil.orchestrators.simple_spine.config import apply_model_override
    from rumil.settings import get_settings

    preset_name = str(overrides.get("preset_name", "")) or "default"
    sub_cfg = get_preset(preset_name)
    # Honor a process-wide single-model override (smoke-test convenience —
    # see Settings.simple_spine_model_override). Applied to the sub-config
    # so this child's mainline AND its subroutines AND any further nested
    # children all use the override.
    model_override = get_settings().simple_spine_model_override
    if model_override:
        sub_cfg = apply_model_override(sub_cfg, model_override)
    sub_clock = ctx.budget_clock.carve_child(sub_token_cap)
    # Explicit `output_guidance` wins; fall back to `intent` for the
    # historical "intent doubles as guidance" behavior so callers that
    # only pass `intent` keep working.
    output_guidance = str(overrides.get("output_guidance") or overrides.get("intent") or "")
    sub_inputs = OrchInputs(
        question_id=child_question.id,
        additional_context=str(overrides.get("additional_context", "")),
        operating_assumptions="",
        output_guidance=output_guidance,
        output_schema=raw_schema,
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
