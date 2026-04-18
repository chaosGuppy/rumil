"""Tests for DraftArtifactCall (src/rumil/calls/draft_artifact.py).

Zero real LLM calls: the structured-call is stubbed everywhere. Uses tmp_db
for real Supabase I/O so we can verify page creation and linking.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from rumil.calls.draft_artifact import (
    DEFAULT_SHAPE,
    DRAFT_ARTIFACT_PROMPT_FILE,
    SUPPORTED_SHAPES,
    ArtifactContext,
    DraftArtifactCall,
    DraftArtifactResult,
    DraftArtifactUpdater,
    _format_system_prompt,
)
from rumil.calls.stages import CallInfra, ContextResult
from rumil.llm import StructuredCallResult, _load_file
from rumil.models import (
    Call,
    CallStatus,
    CallType,
    LinkType,
    Page,
    PageLayer,
    PageLink,
    PageType,
    Workspace,
)
from rumil.moves.base import MoveState
from rumil.tracing.tracer import CallTrace


def _question(
    headline: str = "Will frontier AI automate routine cognitive labour by 2030?",
) -> Page:
    return Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        headline=headline,
        abstract="Whether frontier AI will automate most routine cognitive labour by 2030.",
        content=headline,
    )


def _view_item(headline: str, importance: int = 4, credence: int = 7) -> Page:
    return Page(
        page_type=PageType.VIEW_ITEM,
        layer=PageLayer.WIKI,
        workspace=Workspace.RESEARCH,
        headline=headline,
        content=f"Content for {headline}",
        credence=credence,
        robustness=3,
        importance=importance,
    )


def _canned_result(key_claim_short_ids: list[str]) -> DraftArtifactResult:
    return DraftArtifactResult(
        title="Frontier AI automation by 2030: a cautious brief",
        body_markdown=(
            "## Executive summary\n\n"
            "The evidence suggests integration bottlenecks will delay full "
            "cognitive-labour automation beyond 2030, though capability growth "
            "remains rapid.\n\n"
            "## Key findings\n\n"
            "Three findings anchor this brief, each citing specific view items."
        ),
        key_claims=key_claim_short_ids,
        open_questions=[
            "What is the adoption curve inside incumbent firms?",
            "Which cognitive tasks resist long-horizon agency?",
        ],
    )


@pytest_asyncio.fixture
async def question_with_view(tmp_db):
    """Create a question + view + view items, all linked. Returns dict of ids."""
    q = _question()
    await tmp_db.save_page(q)

    view = Page(
        page_type=PageType.VIEW,
        layer=PageLayer.WIKI,
        workspace=Workspace.RESEARCH,
        headline=f"View: {q.headline}",
        content="",
        sections=["confident_views", "live_hypotheses", "key_uncertainties"],
    )
    await tmp_db.save_page(view)
    await tmp_db.save_link(
        PageLink(
            from_page_id=view.id,
            to_page_id=q.id,
            link_type=LinkType.VIEW_OF,
        )
    )

    items: list[Page] = [
        _view_item("Integration bottleneck dominates capability gains", importance=5, credence=7),
        _view_item("Long-horizon agency remains unreliable", importance=4, credence=6),
        _view_item("Adoption lags in incumbent firms", importance=3, credence=5),
    ]
    for i, item in enumerate(items):
        await tmp_db.save_page(item)
        await tmp_db.save_link(
            PageLink(
                from_page_id=view.id,
                to_page_id=item.id,
                link_type=LinkType.VIEW_ITEM,
                importance=item.importance,
                section="live_hypotheses",
                position=i,
            )
        )

    # Also add one as a CONSIDERATION on the question so build_view surfaces it
    claim = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        headline="Raw capability scaling has slowed since 2024",
        content="Scaling laws appear to have flattened in recent generations.",
        credence=6,
        robustness=3,
        importance=2,
    )
    await tmp_db.save_page(claim)
    await tmp_db.save_link(
        PageLink(
            from_page_id=claim.id,
            to_page_id=q.id,
            link_type=LinkType.CONSIDERATION,
        )
    )

    return {
        "question": q,
        "view": view,
        "items": items,
        "claim": claim,
    }


@pytest_asyncio.fixture
async def draft_call(tmp_db, question_with_view):
    call = Call(
        call_type=CallType.DRAFT_ARTIFACT,
        workspace=Workspace.RESEARCH,
        scope_page_id=question_with_view["question"].id,
        status=CallStatus.PENDING,
    )
    await tmp_db.save_call(call)
    return call


@pytest_asyncio.fixture
async def call_infra(tmp_db, question_with_view, draft_call):
    return CallInfra(
        question_id=question_with_view["question"].id,
        call=draft_call,
        db=tmp_db,
        trace=CallTrace(draft_call.id, tmp_db),
        state=MoveState(draft_call, tmp_db),
    )


def _install_structured_call_stub(mocker, parsed: DraftArtifactResult | None) -> dict:
    """Stub out structured_call in draft_artifact module. Captures invocation args."""
    captured: dict = {}

    async def fake_structured_call(*args, **kwargs):
        captured["system_prompt"] = kwargs.get("system_prompt") or (args[0] if args else None)
        captured["user_message"] = kwargs.get("user_message") or (
            args[1] if len(args) > 1 else None
        )
        captured["response_model"] = kwargs.get("response_model")
        return StructuredCallResult(parsed=parsed, response_text="(stubbed)")

    mocker.patch(
        "rumil.calls.draft_artifact.structured_call",
        side_effect=fake_structured_call,
    )
    return captured


def test_prompt_file_exists_and_mentions_shape_variable():
    text = _load_file(DRAFT_ARTIFACT_PROMPT_FILE)
    assert text.strip()
    assert "{shape}" in text
    for shape in SUPPORTED_SHAPES:
        assert shape in text


def test_call_type_registered_and_not_dispatchable():
    from rumil.models import DISPATCHABLE_CALL_TYPES

    assert CallType.DRAFT_ARTIFACT.value == "draft_artifact"
    assert CallType.DRAFT_ARTIFACT not in DISPATCHABLE_CALL_TYPES


def test_page_type_registered():
    assert PageType.ARTIFACT.value == "artifact"


def test_draft_artifact_call_is_exported():
    from rumil.calls import DraftArtifactCall as Imported

    assert Imported is DraftArtifactCall


def test_draft_artifact_rejects_unknown_shape(tmp_db):
    with pytest.raises(ValueError, match="unsupported shape"):
        DraftArtifactCall(
            "fake-question-id",
            Call(
                call_type=CallType.DRAFT_ARTIFACT,
                workspace=Workspace.RESEARCH,
                status=CallStatus.PENDING,
            ),
            tmp_db,
            shape="not_a_real_shape",  # type: ignore[arg-type]
        )


def test_format_system_prompt_expands_shape_for_each_shape():
    template = "Shape is {shape}. Do the {shape} thing."
    for shape in SUPPORTED_SHAPES:
        out = _format_system_prompt(template, shape)
        assert "{shape}" not in out
        assert shape in out
        assert out.count(shape) == 2


def test_structured_call_system_prompt_includes_shape_for_all_shapes(mocker):
    """Each shape expands into the system prompt the LLM sees."""
    template = _load_file(DRAFT_ARTIFACT_PROMPT_FILE)
    for shape in SUPPORTED_SHAPES:
        rendered = _format_system_prompt(template, shape)
        assert "{shape}" not in rendered
        assert f"`{shape}`" in rendered or shape in rendered


async def test_updater_creates_artifact_page_with_correct_headline_and_content(
    tmp_db, call_infra, question_with_view, mocker
):
    items = question_with_view["items"]
    result = _canned_result([items[0].id[:8], items[1].id[:8]])
    _install_structured_call_stub(mocker, result)

    updater = DraftArtifactUpdater(shape="strategy_brief")
    context = ContextResult(
        context_text="stub context",
        working_page_ids=[question_with_view["question"].id] + [p.id for p in items],
    )

    update_result = await updater.update_workspace(call_infra, context)

    assert len(update_result.created_page_ids) == 1
    artifact_id = update_result.created_page_ids[0]
    artifact = await tmp_db.get_page(artifact_id)
    assert artifact is not None
    assert artifact.page_type == PageType.ARTIFACT
    assert artifact.headline == result.title
    assert artifact.content == result.body_markdown
    assert artifact.provenance_call_id == call_infra.call.id
    assert artifact.extra["shape"] == "strategy_brief"
    assert artifact.extra["open_questions"] == result.open_questions


async def test_updater_creates_related_link_to_source_question(
    tmp_db, call_infra, question_with_view, mocker
):
    items = question_with_view["items"]
    _install_structured_call_stub(mocker, _canned_result([items[0].id[:8]]))

    updater = DraftArtifactUpdater(shape="strategy_brief")
    context = ContextResult(
        context_text="stub",
        working_page_ids=[question_with_view["question"].id, items[0].id],
    )
    update_result = await updater.update_workspace(call_infra, context)

    artifact_id = update_result.created_page_ids[0]
    links = await tmp_db.get_links_from(artifact_id)
    related = [lk for lk in links if lk.link_type == LinkType.RELATED]
    assert len(related) == 1
    assert related[0].to_page_id == question_with_view["question"].id
    assert related[0].reasoning == "Artifact drafted from view"


async def test_updater_creates_cites_links_for_each_key_claim(
    tmp_db, call_infra, question_with_view, mocker
):
    items = question_with_view["items"]
    short_ids = [items[0].id[:8], items[1].id[:8], items[2].id[:8]]
    _install_structured_call_stub(mocker, _canned_result(short_ids))

    updater = DraftArtifactUpdater(shape="strategy_brief")
    context = ContextResult(
        context_text="stub",
        working_page_ids=[question_with_view["question"].id] + [p.id for p in items],
    )
    update_result = await updater.update_workspace(call_infra, context)

    artifact_id = update_result.created_page_ids[0]
    links = await tmp_db.get_links_from(artifact_id)
    cites = [lk for lk in links if lk.link_type == LinkType.CITES]
    cited_ids = {lk.to_page_id for lk in cites}
    assert cited_ids == {p.id for p in items}


async def test_empty_view_produces_minimal_artifact_flagging_absence(tmp_db, mocker):
    """When the View is empty, the prompt explicitly tells the LLM to say so.
    We verify the context builder communicates 'no distilled view' clearly."""
    q = _question("A question with no research yet")
    await tmp_db.save_page(q)

    call = Call(
        call_type=CallType.DRAFT_ARTIFACT,
        workspace=Workspace.RESEARCH,
        scope_page_id=q.id,
        status=CallStatus.PENDING,
    )
    await tmp_db.save_call(call)

    infra = CallInfra(
        question_id=q.id,
        call=call,
        db=tmp_db,
        trace=CallTrace(call.id, tmp_db),
        state=MoveState(call, tmp_db),
    )

    builder = ArtifactContext()
    context = await builder.build_context(infra)
    assert "no distilled view is available" in context.context_text.lower()

    minimal_result = DraftArtifactResult(
        title="No view available",
        body_markdown=(
            "No distilled view is available for this question. "
            "No artifact can responsibly be drafted yet."
        ),
        key_claims=[],
        open_questions=["What should be investigated first?"],
    )
    captured = _install_structured_call_stub(mocker, minimal_result)

    updater = DraftArtifactUpdater(shape="strategy_brief")
    result = await updater.update_workspace(infra, context)

    assert "no distilled view is available" in captured["user_message"].lower()

    artifact = await tmp_db.get_page(result.created_page_ids[0])
    assert artifact is not None
    assert artifact.page_type == PageType.ARTIFACT
    assert "no distilled view is available" in artifact.content.lower()

    links = await tmp_db.get_links_from(artifact.id)
    cites = [lk for lk in links if lk.link_type == LinkType.CITES]
    related = [lk for lk in links if lk.link_type == LinkType.RELATED]
    assert len(cites) == 0
    assert len(related) == 1


async def test_updater_raises_when_llm_returns_no_parsed_output(
    tmp_db, call_infra, question_with_view, mocker
):
    _install_structured_call_stub(mocker, None)

    updater = DraftArtifactUpdater(shape="strategy_brief")
    context = ContextResult(
        context_text="stub",
        working_page_ids=[question_with_view["question"].id],
    )
    with pytest.raises(ValueError, match="no parseable artifact"):
        await updater.update_workspace(call_infra, context)


async def test_call_runner_wires_correct_stages(tmp_db, question_with_view, draft_call):
    runner = DraftArtifactCall(
        question_with_view["question"].id,
        draft_call,
        tmp_db,
        shape="scenario_forecast",
    )
    assert runner.call_type == CallType.DRAFT_ARTIFACT
    assert runner.context_builder.__class__.__name__ == "ArtifactContext"
    assert runner.workspace_updater.__class__.__name__ == "DraftArtifactUpdater"
    assert runner.closing_reviewer.__class__.__name__ == "StandardClosingReview"
    assert runner._shape == "scenario_forecast"
    task = runner.task_description()
    assert "scenario_forecast" in task
    assert question_with_view["question"].id in task


async def test_cli_draft_artifact_flag_fires_call(tmp_db, question_with_view, mocker):
    """End-to-end CLI dispatch: --draft-artifact <qid> --shape <s> invokes DraftArtifactCall."""
    import main as main_module

    items = question_with_view["items"]
    canned = _canned_result([items[0].id[:8]])

    captured_runs: list[dict] = []

    class FakeRunner:
        def __init__(self, question_id, call, db, **kwargs):
            captured_runs.append(
                {
                    "question_id": question_id,
                    "call_id": call.id,
                    "call_type": call.call_type,
                    "shape": kwargs.get("shape"),
                }
            )
            self.update_result = None
            self._question_id = question_id
            self._call = call
            self._db = db

        async def run(self):
            artifact = Page(
                page_type=PageType.ARTIFACT,
                layer=PageLayer.WIKI,
                workspace=Workspace.RESEARCH,
                headline=canned.title,
                content=canned.body_markdown,
                provenance_call_id=self._call.id,
                provenance_call_type=self._call.call_type.value,
            )
            await self._db.save_page(artifact)
            from rumil.calls.stages import UpdateResult

            self.update_result = UpdateResult(
                created_page_ids=[artifact.id],
                moves=[],
                all_loaded_ids=[],
                rounds_completed=1,
            )

    mocker.patch.object(main_module, "DraftArtifactCall", FakeRunner)
    mocker.patch.object(main_module.DB, "create_run", AsyncMock(return_value=None))

    await main_module.cmd_draft_artifact(
        question_with_view["question"].id,
        "market_research",
        tmp_db,
    )

    assert len(captured_runs) == 1
    run = captured_runs[0]
    assert run["question_id"] == question_with_view["question"].id
    assert run["call_type"] == CallType.DRAFT_ARTIFACT
    assert run["shape"] == "market_research"


async def test_default_shape_is_strategy_brief():
    assert DEFAULT_SHAPE == "strategy_brief"
    assert DEFAULT_SHAPE in SUPPORTED_SHAPES
