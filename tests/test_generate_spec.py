"""Tests for add_spec_item move and the GENERATE_SPEC call end-to-end.

- Move-level (non-LLM): add_spec_item creates a hidden SPEC_ITEM page and a
  SPEC_OF link from the spec item to the call's scope_page_id; it errors
  cleanly when scope is unset.
- Call-level (LLM-gated): GENERATE_SPEC produces at least one SPEC_ITEM
  linked via SPEC_OF to the scope question.
"""

import pytest
import pytest_asyncio

from rumil.calls.generate_spec import GenerateSpecCall
from rumil.models import (
    Call,
    CallStatus,
    CallType,
    LinkType,
    Page,
    PageLayer,
    PageType,
    Workspace,
)
from rumil.moves.add_spec_item import AddSpecItemPayload, execute


@pytest_asyncio.fixture
async def artefact_task(tmp_db):
    """Hidden question that stands in for an artefact-task anchor."""
    page = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=(
            "Write a concise onboarding checklist for a new data scientist "
            "joining a small research team. Cover tooling access, how work is "
            "prioritised, and where prior analyses live."
        ),
        headline="Onboarding checklist for a new data scientist",
        hidden=True,
    )
    await tmp_db.save_page(page)
    return page


@pytest_asyncio.fixture
async def generate_spec_call(tmp_db, artefact_task):
    call = Call(
        call_type=CallType.GENERATE_SPEC,
        workspace=Workspace.RESEARCH,
        scope_page_id=artefact_task.id,
        status=CallStatus.PENDING,
    )
    await tmp_db.save_call(call)
    return call


async def test_add_spec_item_creates_hidden_page_and_spec_of_link(
    tmp_db, artefact_task, generate_spec_call
):
    """The move creates a hidden SPEC_ITEM and links SPEC_OF to the scope question."""
    payload = AddSpecItemPayload(
        headline="Name every owner and trigger",
        content=(
            "For each onboarding step, name the person who owns it and the "
            "trigger that starts it (e.g. first-day ticket opened)."
        ),
    )

    result = await execute(payload, generate_spec_call, tmp_db)
    assert result.created_page_id is not None

    created = await tmp_db.get_page(result.created_page_id)
    assert created is not None
    assert created.page_type == PageType.SPEC_ITEM
    assert created.hidden is True
    assert created.headline == payload.headline

    links_from_spec = await tmp_db.get_links_from(result.created_page_id)
    spec_of_links = [l for l in links_from_spec if l.link_type == LinkType.SPEC_OF]
    assert len(spec_of_links) == 1
    assert spec_of_links[0].to_page_id == artefact_task.id


async def test_add_spec_item_errors_without_scope(tmp_db):
    """The move must error (not create a page) when the call has no scope."""
    call = Call(
        call_type=CallType.GENERATE_SPEC,
        workspace=Workspace.RESEARCH,
        scope_page_id=None,
        status=CallStatus.PENDING,
    )
    await tmp_db.save_call(call)

    payload = AddSpecItemPayload(
        headline="Some rule",
        content="Some rule about the artefact.",
    )
    result = await execute(payload, call, tmp_db)

    assert result.created_page_id is None
    assert "scope_page_id" in result.message

    spec_items = await tmp_db.get_pages(
        page_type=PageType.SPEC_ITEM,
        include_hidden=True,
    )
    assert spec_items == []


@pytest.mark.integration
async def test_generate_spec_end_to_end(tmp_db, artefact_task, generate_spec_call):
    """GENERATE_SPEC runs to completion and produces at least one SPEC_ITEM
    linked via SPEC_OF to the artefact-task question."""
    call = GenerateSpecCall(artefact_task.id, generate_spec_call, tmp_db)
    await call.run()

    refreshed = await tmp_db.get_call(generate_spec_call.id)
    assert refreshed.status == CallStatus.COMPLETE

    links_to_task = await tmp_db.get_links_to(artefact_task.id)
    spec_of_links = [l for l in links_to_task if l.link_type == LinkType.SPEC_OF]
    assert len(spec_of_links) >= 1

    spec_item_ids = [l.from_page_id for l in spec_of_links]
    pages = await tmp_db.get_pages_by_ids(spec_item_ids)
    for spec in pages.values():
        assert spec.page_type == PageType.SPEC_ITEM
        assert spec.hidden is True
