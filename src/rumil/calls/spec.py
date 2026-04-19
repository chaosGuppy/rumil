"""Declarative CallSpec: each call type as a frozen data model.

Phase 0 of the CallSpec refactor. This module introduces the contract —
CallSpec, StageRef, AllowedMoves — and the three stage registries that
SpecCallRunner will consult. Nothing depends on it yet: the 34 call-type
modules in this package are still the active source of truth. A
follow-up phase introduces ``SpecCallRunner`` (a generic CallRunner that
takes a CallSpec) and converts the imperative subclasses one by one.

Motivation is in the master plan: today every call type duplicates the
same imperative wiring (context_builder_cls, workspace_updater_cls,
closing_reviewer_cls, prompt file name, preset key, dispatch tool
name, emitted page types) across 34 mostly-interchangeable modules.
Collapsing that into declarative specs keyed by ``(call_type,
variant)`` gives us a single place to query for "which call types are
dispatchable?", "which page types does this call emit?", "which prompt
does it use?" — and makes per-spec A/B and auto-generated docs
tractable.

This file is deliberately additive. Nothing here yet overrides the
legacy registries in ``call_registry.py`` / ``dispatches.py``. Once
``SpecCallRunner`` lands, those registries start deriving their entries
from ``SPECS``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict

from rumil.models import CallType, MoveType, PageType


class StageRef(BaseModel):
    """Reference to a pluggable stage (context builder / updater / reviewer).

    ``id`` is a key into the per-stage registry in this module. ``config``
    is a dict of literal values and/or sentinel objects (``FromCallParam``,
    ``FromSettings``, ``FromStageCtx``) that the stage factory resolves at
    call-construction time.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    id: str
    config: dict[str, Any] = {}


@dataclass(frozen=True)
class FromCallParam:
    """Resolve a stage config value from the call's runtime ``call_params``."""

    name: str
    default: Any = None


@dataclass(frozen=True)
class FromSettings:
    """Resolve a stage config value from the global ``settings`` singleton."""

    name: str


@dataclass(frozen=True)
class FromStageCtx:
    """Resolve a stage config value from the ``StageBuildCtx`` (e.g. question_id, source_page)."""

    attr: str


@dataclass(frozen=True)
class PresetKey:
    """Look up allowed moves from the named ``PRESETS`` entry for this call type."""

    key: str


@dataclass(frozen=True)
class PresetOverlay:
    """Apply a named preset, then add/remove specific moves.

    For rare compositional needs where a spec wants to reuse a preset but
    append an extra move or drop one. Kept narrow: no re-ordering,
    no conditional logic.
    """

    base: PresetKey
    add: tuple[MoveType, ...] = ()
    remove: tuple[MoveType, ...] = ()


AllowedMoves = PresetKey | PresetOverlay | tuple[MoveType, ...]


@dataclass(frozen=True)
class StageBuildCtx:
    """Per-call inputs needed to realize stages from a spec.

    ``runner_factory`` / the ``SpecCallRunner`` constructor populates
    this from the active ``CallInfra`` + any call-type-specific inputs
    (source_page for ingest, allowed_domains for web_research, etc.) and
    passes it to each registered stage factory alongside the resolved
    config dict.
    """

    call_type: CallType
    question_id: str | None
    task_description: str
    available_moves: Sequence[MoveType]
    source_page: Any = None
    extras: dict[str, Any] = None  # type: ignore[assignment]


StageFactory = Callable[[StageBuildCtx, dict[str, Any]], Any]


CONTEXT_BUILDERS: dict[str, StageFactory] = {}
WORKSPACE_UPDATERS: dict[str, StageFactory] = {}
CLOSING_REVIEWERS: dict[str, StageFactory] = {}


def register_context_builder(stage_id: str) -> Callable[[StageFactory], StageFactory]:
    def deco(factory: StageFactory) -> StageFactory:
        if stage_id in CONTEXT_BUILDERS:
            raise ValueError(f"context builder id already registered: {stage_id}")
        CONTEXT_BUILDERS[stage_id] = factory
        return factory

    return deco


def register_workspace_updater(stage_id: str) -> Callable[[StageFactory], StageFactory]:
    def deco(factory: StageFactory) -> StageFactory:
        if stage_id in WORKSPACE_UPDATERS:
            raise ValueError(f"workspace updater id already registered: {stage_id}")
        WORKSPACE_UPDATERS[stage_id] = factory
        return factory

    return deco


def register_closing_reviewer(stage_id: str) -> Callable[[StageFactory], StageFactory]:
    def deco(factory: StageFactory) -> StageFactory:
        if stage_id in CLOSING_REVIEWERS:
            raise ValueError(f"closing reviewer id already registered: {stage_id}")
        CLOSING_REVIEWERS[stage_id] = factory
        return factory

    return deco


class CallSpec(BaseModel):
    """Declarative specification of a single call type (+ variant).

    Subclass of pydantic ``BaseModel`` so specs serialize to JSON for the
    future ``/api/call-specs`` endpoint and per-spec docs.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    call_type: CallType
    variant: str = "default"
    description: str

    task_template: str | None = None
    """Full task-description template; ``{scope_id}`` is substituted.

    When set, overrides the default "description + scope label" format
    used by ``SpecCallRunner.task_description``. Use sparingly — only
    when a call type's wording around the scope ID genuinely diverges
    from the convention (e.g. ``find_considerations``'s "Question ID
    (use this when linking considerations): ..." phrasing).
    """

    prompt_id: str
    prompt_version: str | None = None

    context_builder: StageRef
    workspace_updater: StageRef
    closing_reviewer: StageRef

    allowed_moves: AllowedMoves

    dispatchable: bool = False
    dispatch_tool_name: str | None = None
    dispatch_tool_description: str | None = None
    dispatch_payload_schema: type[BaseModel] | None = None

    emits_page_types: frozenset[PageType] = frozenset()
    scope_page_type: PageType = PageType.QUESTION

    estimated_budget_cost: int = 1
    eval_gates: tuple[str, ...] = ()

    runner_factory: Callable[..., Any] | None = None


SpecKey = tuple[CallType, str]
SPECS: dict[SpecKey, CallSpec] = {}


def register_spec(spec: CallSpec) -> CallSpec:
    """Register a CallSpec. Raises on duplicate (call_type, variant)."""
    key: SpecKey = (spec.call_type, spec.variant)
    if key in SPECS:
        raise ValueError(f"CallSpec already registered for {key!r}")
    SPECS[key] = spec
    return spec


def get_spec(call_type: CallType, variant: str = "default") -> CallSpec | None:
    """Return the registered spec for a (call_type, variant), or None."""
    return SPECS.get((call_type, variant))


def dispatchable_call_types() -> set[CallType]:
    """Return the set of call types that any registered spec marks as dispatchable.

    Once the legacy ``DISPATCHABLE_CALL_TYPES`` constant is retired this
    becomes the single source of truth for dispatch gating.
    """
    return {spec.call_type for spec in SPECS.values() if spec.dispatchable}
