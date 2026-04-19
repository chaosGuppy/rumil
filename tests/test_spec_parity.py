"""Contract: every spec-registered CallType produces the same task
description + allowed moves as its legacy imperative subclass. If these
diverge we've mis-translated the spec and shouldn't flip the registry
over to SpecCallRunner for that call type yet.
"""

from __future__ import annotations

import pytest

from rumil.calls import spec_registry  # noqa: F401  (side-effect)
from rumil.calls.call_registry import ASSESS_CALL_CLASSES, CALL_RUNNER_CLASSES
from rumil.calls.spec import SPECS, SpecKey
from rumil.calls.spec_runner import SpecCallRunner
from rumil.models import Call, CallStatus, CallType, Workspace


def _legacy_class_for(call_type: CallType, variant: str):
    """Return the imperative CallRunner subclass for a (call_type, variant).

    ``ASSESS`` is the one call type with multiple legacy variants
    (AssessCall vs BigAssessCall); other call types have a single entry
    in ``CALL_RUNNER_CLASSES`` keyed by ``CallType``.
    """
    if call_type == CallType.ASSESS:
        return ASSESS_CALL_CLASSES.get(variant)
    return CALL_RUNNER_CLASSES.get(call_type)


def _spec_keys() -> list[SpecKey]:
    keys: list[SpecKey] = list(SPECS.keys())
    keys.sort(key=lambda k: k[0].value)
    return keys


@pytest.fixture(params=_spec_keys())
async def spec_and_legacy(request, tmp_db, question_page):
    param: tuple[CallType, str] = request.param
    call_type, variant = param
    legacy_cls = _legacy_class_for(call_type, variant)
    if legacy_cls is None:
        pytest.skip(f"no legacy class for {call_type.value} variant={variant!r}")

    call = Call(
        call_type=call_type,
        workspace=Workspace.RESEARCH,
        scope_page_id=question_page.id,
        status=CallStatus.PENDING,
    )
    await tmp_db.save_call(call)

    # Some legacy subclasses require extra kwargs (e.g. find_considerations
    # requires max_rounds/fruit_threshold in __init__); feed the defaults
    # that SpecCallRunner itself uses so the comparison is apples-to-apples.
    extra_kwargs: dict = {}
    if call_type == CallType.FIND_CONSIDERATIONS:
        extra_kwargs = {"max_rounds": 5, "fruit_threshold": 4}
    legacy = legacy_cls(
        question_id=question_page.id,
        call=call,
        db=tmp_db,
        **extra_kwargs,
    )

    spec = SPECS[(call_type, variant)]
    spec_runner = SpecCallRunner(
        spec,
        question_id=question_page.id,
        call=call,
        db=tmp_db,
    )
    return call_type, legacy, spec_runner


async def test_task_description_matches(spec_and_legacy):
    call_type, legacy, spec_runner = spec_and_legacy
    legacy_desc = legacy.task_description().strip()
    spec_desc = spec_runner.task_description().strip()
    assert legacy_desc == spec_desc, (
        f"{call_type.value}: legacy vs spec descriptions diverge\n"
        f"--- legacy ---\n{legacy_desc}\n--- spec ---\n{spec_desc}"
    )


async def test_allowed_moves_match(spec_and_legacy):
    call_type, legacy, spec_runner = spec_and_legacy
    legacy_moves = list(legacy._resolve_available_moves())
    spec_moves = list(spec_runner._resolve_available_moves())
    assert set(legacy_moves) == set(spec_moves), (
        f"{call_type.value}: legacy vs spec allowed-moves diverge\n"
        f"legacy_only: {set(legacy_moves) - set(spec_moves)}\n"
        f"spec_only:   {set(spec_moves) - set(legacy_moves)}"
    )


async def test_stage_types_match(spec_and_legacy):
    """Spec-resolved stage types match the legacy subclass's stage types."""
    call_type, legacy, spec_runner = spec_and_legacy
    assert type(spec_runner.context_builder) is type(legacy.context_builder), (
        f"{call_type.value}: context_builder types differ "
        f"(legacy={type(legacy.context_builder).__name__}, "
        f"spec={type(spec_runner.context_builder).__name__})"
    )
    assert type(spec_runner.workspace_updater) is type(legacy.workspace_updater), (
        f"{call_type.value}: workspace_updater types differ "
        f"(legacy={type(legacy.workspace_updater).__name__}, "
        f"spec={type(spec_runner.workspace_updater).__name__})"
    )
    assert type(spec_runner.closing_reviewer) is type(legacy.closing_reviewer), (
        f"{call_type.value}: closing_reviewer types differ "
        f"(legacy={type(legacy.closing_reviewer).__name__}, "
        f"spec={type(spec_runner.closing_reviewer).__name__})"
    )


async def test_ingest_spec_parity_with_source_page(tmp_db, question_page):
    """Ingest isn't in CALL_RUNNER_CLASSES (different dispatch path), so the
    generic parity fixture skips it. Check spec↔legacy parity directly by
    constructing both with a synthetic source page.
    """
    from rumil.calls.closing_reviewers import IngestClosingReview
    from rumil.calls.context_builders import IngestEmbeddingContext
    from rumil.calls.ingest import IngestCall
    from rumil.calls.page_creators import SimpleAgentLoop
    from rumil.models import Page, PageLayer, PageType, Workspace

    source_page = Page(
        page_type=PageType.SOURCE,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="source body",
        headline="a source document",
        extra={"filename": "sample.pdf"},
    )
    await tmp_db.save_page(source_page)

    call = Call(
        call_type=CallType.INGEST,
        workspace=Workspace.RESEARCH,
        scope_page_id=question_page.id,
        status=CallStatus.PENDING,
    )
    await tmp_db.save_call(call)

    legacy = IngestCall(source_page, question_page.id, call, tmp_db)

    spec = SPECS[(CallType.INGEST, "default")]
    spec_runner = SpecCallRunner(
        spec,
        question_id=question_page.id,
        call=call,
        db=tmp_db,
        stage_ctx_extras={"source_page": source_page},
    )

    assert legacy.task_description().strip() == spec_runner.task_description().strip()
    assert set(legacy._resolve_available_moves()) == set(spec_runner._resolve_available_moves())
    assert isinstance(spec_runner.context_builder, IngestEmbeddingContext)
    assert isinstance(spec_runner.workspace_updater, SimpleAgentLoop)
    assert isinstance(spec_runner.closing_reviewer, IngestClosingReview)
    # filename propagated from source page's extra
    assert spec_runner.closing_reviewer._filename == "sample.pdf"
