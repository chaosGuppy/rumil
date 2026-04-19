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


def test_capabilities_endpoint_returns_registry_content() -> None:
    """The /api/capabilities endpoint must return registry content verbatim.

    If this test is updated to expect different keys, the frontend is
    almost certainly affected — check the UI too.
    """
    import asyncio

    from rumil.api.app import get_capabilities

    caps = asyncio.run(get_capabilities())
    variants = {o["variant"] for o in caps["orchestrators"]}
    assert variants == set(ORCHESTRATORS)

    eval_names = {e["name"] for e in caps["eval_types"]}
    assert eval_names == set(EVALUATION_TYPES)

    pipeline_names = {p["name"] for p in caps["grounding_pipelines"]}
    assert pipeline_names == set(GROUNDING_PIPELINES)
