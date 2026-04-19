"""Contract tests for the orchestrator + evaluation registries.

Purpose: every surface (CLI, API, chat, skills) now reads from these
registries, so drift between a new registry entry and its consumers is a
bug class we should catch at test time rather than at runtime.

These tests are intentionally structural — they don't exercise orchestrator
behaviour, they just assert that each registered entry is complete and that
the dispatch layer + chat catalog stay in sync with the registry.
"""

import inspect
from pathlib import Path

import pytest

from rumil.evaluate.registry import (
    EVALUATION_TYPES,
    GROUNDING_PIPELINES,
    get_evaluation_type_spec,
    get_grounding_pipeline_spec,
)
from rumil.orchestrators.registry import (
    ORCHESTRATORS,
    build_orchestrator,
    get_orchestrator_spec,
)

_PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"

_VALID_STABILITY = {"stable", "experimental", "cli_only"}
_VALID_COST_BAND = {"low", "medium", "high"}


@pytest.mark.parametrize("variant", sorted(ORCHESTRATORS))
def test_orchestrator_spec_is_well_formed(variant: str) -> None:
    spec = ORCHESTRATORS[variant]
    assert spec.variant == variant, "key must match spec.variant"
    assert spec.description.strip(), "every orchestrator needs a non-empty description"
    assert spec.stability in _VALID_STABILITY, f"stability must be one of {_VALID_STABILITY}"
    assert spec.cost_band in _VALID_COST_BAND, f"cost_band must be one of {_VALID_COST_BAND}"
    assert callable(spec.factory)


@pytest.mark.parametrize("variant", sorted(ORCHESTRATORS))
def test_orchestrator_factory_produces_runnable(variant: str) -> None:
    class _FakeDB:
        run_id = "fake-run-id"
        project_id = ""

    orch = ORCHESTRATORS[variant].factory(_FakeDB(), None)  # type: ignore[arg-type]
    run_method = getattr(orch, "run", None)
    assert callable(run_method), f"{variant} factory must produce .run()"
    assert inspect.iscoroutinefunction(run_method), f"{variant}.run must be async"


def test_get_orchestrator_spec_raises_for_unknown() -> None:
    with pytest.raises(ValueError, match="Unknown prioritizer_variant"):
        get_orchestrator_spec("no-such-variant")


def test_build_orchestrator_honors_variant_override() -> None:
    class _FakeDB:
        run_id = "fake-run-id"
        project_id = ""

    orch = build_orchestrator(_FakeDB(), None, variant="critique_first")  # type: ignore[arg-type]
    assert type(orch).__name__ == "CritiqueFirstOrchestrator"


def test_refine_artifact_skips_global_prio_wrapping() -> None:
    class _FakeDB:
        run_id = "fake-run-id"
        project_id = ""

    orch = build_orchestrator(
        _FakeDB(),  # type: ignore[arg-type]
        None,
        variant="refine_artifact",
        enable_global_prio=True,
    )
    assert type(orch).__name__ == "RefineArtifactOrchestrator"


@pytest.mark.parametrize("name", sorted(EVALUATION_TYPES))
def test_evaluation_type_prompt_files_exist(name: str) -> None:
    spec = EVALUATION_TYPES[name]
    assert spec.description.strip()
    assert (_PROMPTS_DIR / spec.prompt_file).exists(), (
        f"{name}: missing prompt file {spec.prompt_file}"
    )
    assert (_PROMPTS_DIR / spec.investigator_prompt_file).exists(), (
        f"{name}: missing investigator prompt file {spec.investigator_prompt_file}"
    )


def test_get_evaluation_type_spec_raises_for_unknown() -> None:
    with pytest.raises(ValueError, match="Unknown eval_type"):
        get_evaluation_type_spec("no-such-type")


@pytest.mark.parametrize("name", sorted(GROUNDING_PIPELINES))
def test_grounding_pipeline_spec_is_well_formed(name: str) -> None:
    spec = GROUNDING_PIPELINES[name]
    assert spec.name == name
    assert spec.description.strip()
    assert spec.recommended_eval_type in EVALUATION_TYPES, (
        f"{name} recommends unknown eval_type {spec.recommended_eval_type!r}"
    )
    assert inspect.iscoroutinefunction(spec.runner), f"{name} runner must be an async function"


def test_get_grounding_pipeline_spec_raises_for_unknown() -> None:
    with pytest.raises(ValueError, match="Unknown grounding pipeline"):
        get_grounding_pipeline_spec("no-such-pipeline")


def test_chat_orchestrate_tool_variants_match_registry() -> None:
    """Chat's 'orchestrate' tool rejects variants not in the registry.

    Regression guard: if a new orchestrator is registered but chat's
    variant validation diverges, the orchestrate tool would silently
    accept/reject the wrong set. This test keeps chat + registry linked.
    """
    from rumil.api import chat

    exposed = {v for v, spec in ORCHESTRATORS.items() if spec.exposed_in_chat}
    chat_exposed = {v for v, spec in chat.ORCHESTRATORS.items() if spec.exposed_in_chat}
    assert chat_exposed == exposed, (
        "chat and registry disagree about which orchestrator variants are "
        "exposed_in_chat — re-import or registry is stale"
    )


def test_chat_async_sentinels_have_handlers() -> None:
    """Every sentinel the chat tools emit has a corresponding _ASYNC_HANDLERS entry."""
    from rumil.api import chat

    tool_sentinels = {
        "__async_dispatch__",
        "__async_orchestrate__",
        "__async_ingest__",
        "__async_evaluate__",
        "__async_ground__",
    }
    assert tool_sentinels <= set(chat._ASYNC_HANDLERS), (
        "Every chat tool sentinel must have a handler in _ASYNC_HANDLERS. "
        f"Missing: {tool_sentinels - set(chat._ASYNC_HANDLERS)}"
    )


def test_every_dispatchable_call_type_has_a_runner_class() -> None:
    """Every ``CallType`` in ``DISPATCHABLE_CALL_TYPES`` must have a registered
    runner in ``CALL_RUNNER_CLASSES``.

    Regression guard: when a new dispatchable scout / assess variant is added
    to the enum, we need the runner mapping in the same PR or
    ``get_call_runner_class`` raises at runtime from chat, CLI, and any other
    dispatch site.
    """
    from rumil.calls.call_registry import CALL_RUNNER_CLASSES
    from rumil.models import DISPATCHABLE_CALL_TYPES

    missing = set(DISPATCHABLE_CALL_TYPES) - set(CALL_RUNNER_CLASSES)
    assert not missing, (
        f"Dispatchable CallTypes with no registered runner: "
        f"{sorted(ct.value for ct in missing)}. "
        "Add entries to rumil.calls.call_registry.CALL_RUNNER_CLASSES."
    )

    stray = set(CALL_RUNNER_CLASSES) - set(DISPATCHABLE_CALL_TYPES)
    assert not stray, (
        f"CALL_RUNNER_CLASSES has runners for non-dispatchable CallTypes: "
        f"{sorted(ct.value for ct in stray)}. Either add to "
        "DISPATCHABLE_CALL_TYPES or drop the runner entry."
    )


def test_every_trace_event_is_surfaced_or_suppressed() -> None:
    """Every variant in ``rumil.tracing.trace_events.TraceEvent`` must be
    categorized in ``subscribe.py`` — either surfaced to the chat SSE
    stream or explicitly suppressed.

    Regression guard: if someone adds a new TraceEvent type but forgets
    to update subscribe.py, the event silently drops off the chat stream.
    This test fails fast in that case.
    """
    import typing

    from rumil.tracing import trace_events
    from rumil.tracing.subscribe import _SUPPRESSED_EVENTS, _SURFACED_EVENTS

    union_members = typing.get_args(typing.get_args(trace_events.TraceEvent)[0])
    all_event_values: set[str] = set()
    for member in union_members:
        literal_field = member.model_fields["event"]
        literal_values = typing.get_args(literal_field.annotation)
        all_event_values.update(literal_values)

    assert all_event_values, (
        "Expected to extract at least one event value from TraceEvent — "
        "union introspection may have broken"
    )

    overlap = _SURFACED_EVENTS & _SUPPRESSED_EVENTS
    assert not overlap, f"event in both surface and suppress sets: {overlap}"

    categorized = _SURFACED_EVENTS | _SUPPRESSED_EVENTS
    uncategorized = all_event_values - categorized
    assert not uncategorized, (
        f"TraceEvent variants not categorized in subscribe.py: {uncategorized}. "
        "Add each to either _SURFACED_EVENTS or _SUPPRESSED_EVENTS."
    )

    stray = categorized - all_event_values
    assert not stray, f"subscribe.py references events that no longer exist in TraceEvent: {stray}"


def test_capabilities_endpoint_returns_registry_content() -> None:
    """The /api/capabilities endpoint must return registry content verbatim.

    If this test is updated to expect different keys, the frontend is
    almost certainly affected — check the UI too.
    """
    import asyncio

    from rumil.api.app import get_capabilities

    caps = asyncio.run(get_capabilities())
    variants = {o.variant for o in caps.orchestrators}
    assert variants == set(ORCHESTRATORS)

    eval_names = {e.name for e in caps.eval_types}
    assert eval_names == set(EVALUATION_TYPES)

    pipeline_names = {p.name for p in caps.grounding_pipelines}
    assert pipeline_names == set(GROUNDING_PIPELINES)
