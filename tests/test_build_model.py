"""Tests for BuildModelCall: theoretical model building."""

import pytest
import pytest_asyncio

from rumil.calls.build_model import BuildModelCall
from rumil.models import (
    Call,
    CallStatus,
    CallType,
    LinkType,
    ModelFlavor,
    MoveType,
    Page,
    PageLayer,
    PageLink,
    PageType,
    Workspace,
)
from rumil.moves.base import MoveState
from rumil.moves.registry import MOVES
from rumil.moves.write_model_body import WriteModelBodyPayload


def _question(headline: str = "What drives adoption of new software?", **kw) -> Page:
    defaults = {
        "page_type": PageType.QUESTION,
        "layer": PageLayer.SQUIDGY,
        "workspace": Workspace.RESEARCH,
        "content": headline,
        "headline": headline,
    }
    defaults.update(kw)
    return Page(**defaults)


@pytest_asyncio.fixture
async def build_model_call(tmp_db, question_page):
    call = Call(
        call_type=CallType.BUILD_MODEL,
        workspace=Workspace.RESEARCH,
        scope_page_id=question_page.id,
        status=CallStatus.PENDING,
    )
    await tmp_db.save_call(call)
    return call


async def test_create_model_page_and_model_of_link(tmp_db, question_page, build_model_call):
    """The call pre-creates a MODEL page linked to the question via MODEL_OF."""
    runner = BuildModelCall(question_page.id, build_model_call, tmp_db)
    model_id = await runner._create_model_page()

    page = await tmp_db.get_page(model_id)
    assert page is not None
    assert page.page_type == PageType.MODEL
    assert page.headline.startswith("Model: ")
    assert page.headline.endswith(question_page.headline)
    assert page.extra["flavor"] == "theoretical"

    links = await tmp_db.get_links_to(question_page.id)
    model_of_links = [l for l in links if l.link_type == LinkType.MODEL_OF]
    assert len(model_of_links) == 1
    assert model_of_links[0].from_page_id == model_id


async def test_create_model_page_supersedes_prior_model(tmp_db, question_page, build_model_call):
    """Running build_model twice supersedes the old MODEL page."""
    runner_1 = BuildModelCall(question_page.id, build_model_call, tmp_db)
    old_model_id = await runner_1._create_model_page()

    call_2 = Call(
        call_type=CallType.BUILD_MODEL,
        workspace=Workspace.RESEARCH,
        scope_page_id=question_page.id,
        status=CallStatus.PENDING,
    )
    await tmp_db.save_call(call_2)
    runner_2 = BuildModelCall(question_page.id, call_2, tmp_db)
    new_model_id = await runner_2._create_model_page()

    assert new_model_id != old_model_id

    old_page = await tmp_db.get_page(old_model_id)
    assert old_page is not None
    assert old_page.is_superseded

    new_page = await tmp_db.get_page(new_model_id)
    assert new_page is not None
    assert not new_page.is_superseded


async def test_get_active_model_for_question_none_when_absent(
    tmp_db,
    question_page,
    build_model_call,
):
    runner = BuildModelCall(question_page.id, build_model_call, tmp_db)
    active = await runner._get_active_model_for_question(question_page.id)
    assert active is None


async def test_get_active_model_for_question_skips_superseded(
    tmp_db,
    question_page,
    build_model_call,
):
    runner = BuildModelCall(question_page.id, build_model_call, tmp_db)
    first_id = await runner._create_model_page()

    call_2 = Call(
        call_type=CallType.BUILD_MODEL,
        workspace=Workspace.RESEARCH,
        scope_page_id=question_page.id,
        status=CallStatus.PENDING,
    )
    await tmp_db.save_call(call_2)
    runner_2 = BuildModelCall(question_page.id, call_2, tmp_db)
    second_id = await runner_2._create_model_page()

    active = await runner_2._get_active_model_for_question(question_page.id)
    assert active is not None
    assert active.id == second_id
    assert active.id != first_id


def test_build_model_flavor_captured_in_call_params(tmp_db):
    """The flavor param is recorded on the call so it's visible in traces."""
    q = _question()
    call = Call(
        call_type=CallType.BUILD_MODEL,
        workspace=Workspace.RESEARCH,
        scope_page_id=q.id,
        status=CallStatus.PENDING,
    )

    class _Stub:
        project_id = ""
        run_id = ""
        staged = False

    runner = BuildModelCall.__new__(BuildModelCall)
    BuildModelCall.__init__(runner, q.id, call, _Stub(), flavor=ModelFlavor.THEORETICAL)  # type: ignore[arg-type]
    assert call.call_params is not None
    assert call.call_params["flavor"] == "theoretical"


async def test_write_model_body_move_updates_content_and_robustness(
    tmp_db,
    question_page,
    build_model_call,
):
    """write_model_body fills in the MODEL page body + robustness via mutation events."""
    runner = BuildModelCall(question_page.id, build_model_call, tmp_db)
    model_id = await runner._create_model_page()

    state = MoveState(build_model_call, tmp_db)
    tool = MOVES[MoveType.WRITE_MODEL_BODY].bind(state)

    body = (
        "## Variables\n- N: market size (10k-100k).\n\n"
        "## Relations\n- Bass diffusion.\n\n"
        "## Parameters\n- p=0.03, q=0.15.\n\n"
        "## Predictions\n- P1: time-to-50% is 6-10 quarters.\n\n"
        "## Assumptions\n- A1: N is fixed.\n\n"
        "## Sensitivities\n- P1 most sensitive to q."
    )
    result = await tool.fn(
        {
            "model_page_id": model_id,
            "content": body,
            "robustness": 3,
            "robustness_reasoning": "Relations drawn from Bass diffusion theory.",
        }
    )
    assert "Model body written" in result

    page = await tmp_db.get_page(model_id)
    assert page is not None
    assert page.content == body
    assert page.robustness == 3
    assert "Bass" in (page.robustness_reasoning or "")


async def test_write_model_body_rejects_non_model_target(
    tmp_db,
    question_page,
    build_model_call,
):
    """write_model_body must only write to MODEL pages."""
    state = MoveState(build_model_call, tmp_db)
    tool = MOVES[MoveType.WRITE_MODEL_BODY].bind(state)

    result = await tool.fn(
        {
            "model_page_id": question_page.id,
            "content": "body",
            "robustness": 2,
            "robustness_reasoning": "because",
        }
    )
    assert "ERROR" in result
    assert "expected a model page" in result


def test_write_model_body_payload_requires_all_fields():
    with pytest.raises(Exception):
        WriteModelBodyPayload(model_page_id="abc", content="x", robustness=3)  # type: ignore[call-arg]


def test_model_in_dispatchable_types():
    """build_model must be dispatchable by prioritization."""
    from rumil.models import DISPATCHABLE_CALL_TYPES

    assert CallType.BUILD_MODEL in DISPATCHABLE_CALL_TYPES


def test_build_model_has_available_moves_entry():
    """build_model must be listed in the default moves preset."""
    from rumil.available_moves import PRESETS

    for preset_name, preset in PRESETS.items():
        assert CallType.BUILD_MODEL in preset, f"Preset {preset_name} missing BUILD_MODEL entry"
        moves = preset[CallType.BUILD_MODEL]
        assert MoveType.WRITE_MODEL_BODY in moves


@pytest.mark.integration
async def test_build_model_lifecycle(tmp_db, question_page, build_model_call):
    """End-to-end: the call runs, creates a MODEL page, and completes."""
    await tmp_db.init_budget(4)
    runner = BuildModelCall(question_page.id, build_model_call, tmp_db)
    await runner.run()

    refreshed = await tmp_db.get_call(build_model_call.id)
    assert refreshed.status == CallStatus.COMPLETE
    assert refreshed.completed_at is not None

    links = await tmp_db.get_links_to(question_page.id)
    model_of_links = [l for l in links if l.link_type == LinkType.MODEL_OF]
    assert len(model_of_links) == 1

    model_page = await tmp_db.get_page(model_of_links[0].from_page_id)
    assert model_page is not None
    assert model_page.page_type == PageType.MODEL
    assert not model_page.is_superseded


def test_build_model_dispatch_schema_registered():
    """The BUILD_MODEL call type has a DispatchDef and a handler."""
    from rumil.calls.dispatches import DISPATCH_DEFS
    from rumil.models import BuildModelDispatchPayload
    from rumil.orchestrators.dispatch_handlers import DISPATCH_HANDLERS

    assert CallType.BUILD_MODEL in DISPATCH_DEFS
    assert BuildModelDispatchPayload in DISPATCH_HANDLERS
