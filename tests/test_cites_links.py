"""Tests for CITES link creation when claims cite sources."""

import pytest
import pytest_asyncio

from rumil.calls.ingest import IngestCall
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
from rumil.moves import MOVES
from rumil.moves.base import MoveState
from rumil.moves.create_claim import MoveType


@pytest_asyncio.fixture
async def source_page(tmp_db):
    page = Page(
        page_type=PageType.SOURCE,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="The sky appears blue due to Rayleigh scattering of sunlight.",
        headline="Rayleigh scattering explains blue sky",
        extra={"filename": "sky-science.txt", "char_count": 58},
    )
    await tmp_db.save_page(page)
    return page


@pytest_asyncio.fixture
async def second_source_page(tmp_db):
    page = Page(
        page_type=PageType.SOURCE,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="Shorter wavelengths scatter more in the atmosphere.",
        headline="Wavelength dependence of atmospheric scattering",
        extra={"filename": "optics-101.txt", "char_count": 50},
    )
    await tmp_db.save_page(page)
    return page


async def test_cites_link_created_for_source(tmp_db, scout_call, source_page):
    """A claim with source_ids should get a CITES link and no source_id in extra."""
    state = MoveState(scout_call, tmp_db)
    tool = MOVES[MoveType.CREATE_CLAIM].bind(state)
    await tool.fn({
        "headline": "Blue sky from scattering",
        "content": "Rayleigh scattering causes blue sky.",
        "source_ids": [source_page.id[:8]],
    })

    assert len(state.created_page_ids) == 1
    claim_id = state.created_page_ids[0]

    links = await tmp_db.get_links_from(claim_id)
    cites_links = [l for l in links if l.link_type == LinkType.CITES]
    assert len(cites_links) == 1
    assert cites_links[0].to_page_id == source_page.id

    page = await tmp_db.get_page(claim_id)
    assert "source_id" not in page.extra


async def test_no_cites_link_when_source_ids_empty(tmp_db, scout_call):
    """A claim without source_ids should have no CITES links."""
    state = MoveState(scout_call, tmp_db)
    tool = MOVES[MoveType.CREATE_CLAIM].bind(state)
    await tool.fn({
        "headline": "Unsourced claim",
        "content": "This claim cites nothing.",
    })

    assert len(state.created_page_ids) == 1
    claim_id = state.created_page_ids[0]
    links = await tmp_db.get_links_from(claim_id)
    cites_links = [l for l in links if l.link_type == LinkType.CITES]
    assert len(cites_links) == 0


async def test_multiple_cites_links(
    tmp_db, scout_call, source_page, second_source_page,
):
    """A claim citing multiple sources should get one CITES link per source."""
    state = MoveState(scout_call, tmp_db)
    tool = MOVES[MoveType.CREATE_CLAIM].bind(state)
    await tool.fn({
        "headline": "Combined scattering evidence",
        "content": "Multiple sources confirm scattering.",
        "source_ids": [source_page.id[:8], second_source_page.id[:8]],
    })

    assert len(state.created_page_ids) == 1
    claim_id = state.created_page_ids[0]
    links = await tmp_db.get_links_from(claim_id)
    cites_links = [l for l in links if l.link_type == LinkType.CITES]
    assert len(cites_links) == 2
    cited_ids = {l.to_page_id for l in cites_links}
    assert cited_ids == {source_page.id, second_source_page.id}


async def test_cites_and_consideration_links_coexist(
    tmp_db, scout_call, question_page, source_page,
):
    """A claim with both source_ids and links should create both link types."""
    state = MoveState(scout_call, tmp_db)
    tool = MOVES[MoveType.CREATE_CLAIM].bind(state)
    await tool.fn({
        "headline": "Sourced and linked claim",
        "content": "This claim cites a source and bears on a question.",
        "source_ids": [source_page.id[:8]],
        "links": [{
            "question_id": question_page.id[:8],
            "strength": 3.5,
            "reasoning": "Bears on the question",
        }],
    })

    assert len(state.created_page_ids) == 1
    claim_id = state.created_page_ids[0]
    links = await tmp_db.get_links_from(claim_id)

    cites_links = [l for l in links if l.link_type == LinkType.CITES]
    consideration_links = [l for l in links if l.link_type == LinkType.CONSIDERATION]
    assert len(cites_links) == 1
    assert cites_links[0].to_page_id == source_page.id
    assert len(consideration_links) == 1
    assert consideration_links[0].to_page_id == question_page.id


@pytest.mark.integration
async def test_ingest_creates_cites_links(tmp_db, question_page, source_page):
    """End-to-end ingest: claims should have CITES links to the source."""
    ingest_call = Call(
        call_type=CallType.INGEST,
        workspace=Workspace.RESEARCH,
        scope_page_id=question_page.id,
        status=CallStatus.PENDING,
    )
    await tmp_db.save_call(ingest_call)

    ingest = IngestCall(source_page, question_page.id, ingest_call, tmp_db)
    await ingest.run()

    refreshed = await tmp_db.get_call(ingest_call.id)
    assert refreshed.status == CallStatus.COMPLETE

    source_links = await tmp_db.get_links_to(source_page.id)
    cites_links = [l for l in source_links if l.link_type == LinkType.CITES]
    assert len(cites_links) >= 1, "Ingest should create at least one claim citing the source"

    question_links = await tmp_db.get_links_to(question_page.id)
    consideration_links = [
        l for l in question_links if l.link_type == LinkType.CONSIDERATION
    ]
    assert len(consideration_links) >= 1, (
        "Ingest should create at least one consideration linked to the question"
    )
