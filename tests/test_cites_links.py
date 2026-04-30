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
    content = (
        "A 2024 study by Acemoglu and Restrepo estimates that LLM-based tools "
        "have already automated roughly 5% of routine cognitive labour in "
        "white-collar industries, and projects that share could rise to 25% "
        "by 2030 under continued frontier-model progress. The authors note "
        "that the pace is gated by integration friction (procurement cycles, "
        "regulatory review, internal change management) more than by raw "
        "capability."
    )
    page = Page(
        page_type=PageType.SOURCE,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=content,
        headline="LLMs and the pace of cognitive-labour automation (Acemoglu & Restrepo 2024)",
        extra={"filename": "acemoglu-restrepo-2024.txt", "char_count": len(content)},
    )
    await tmp_db.save_page(page)
    return page


@pytest_asyncio.fixture
async def second_source_page(tmp_db):
    content = (
        "Internal benchmark data from a major consulting firm shows that "
        "junior-level analyst tasks now take 40% less time when augmented "
        "with frontier LLMs, while senior strategy work shows little speedup."
    )
    page = Page(
        page_type=PageType.SOURCE,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=content,
        headline="Consulting-firm benchmark: LLM speedups concentrate at junior levels",
        extra={"filename": "consulting-benchmark.txt", "char_count": len(content)},
    )
    await tmp_db.save_page(page)
    return page


async def test_cites_link_created_for_source(tmp_db, scout_call, source_page):
    """A claim with source_urls should get a CITES link and no source_id in extra."""
    state = MoveState(scout_call, tmp_db)
    tool = MOVES[MoveType.CREATE_CLAIM].bind(state)
    await tool.fn(
        {
            "headline": "LLMs already automate ~5% of routine cognitive labour",
            "content": "Frontier LLMs have already displaced a measurable share of routine white-collar tasks.",
            "credence": 6,
            "credence_reasoning": "Headline figure from a recent Acemoglu/Restrepo estimate.",
            "robustness": 3,
            "robustness_reasoning": "Sourced from a single page; cross-check would firm it.",
            "source_urls": [source_page.id[:8]],
        }
    )

    assert len(state.created_page_ids) == 1
    claim_id = state.created_page_ids[0]

    links = await tmp_db.get_links_from(claim_id)
    cites_links = [l for l in links if l.link_type == LinkType.CITES]
    assert len(cites_links) == 1
    assert cites_links[0].to_page_id == source_page.id

    page = await tmp_db.get_page(claim_id)
    assert "source_id" not in page.extra


async def test_no_cites_link_when_source_urls_empty(tmp_db, scout_call):
    """A claim without source_urls should have no CITES links."""
    state = MoveState(scout_call, tmp_db)
    tool = MOVES[MoveType.CREATE_CLAIM].bind(state)
    await tool.fn(
        {
            "headline": "Unsourced claim",
            "content": "This claim cites nothing.",
            "credence": 5,
            "credence_reasoning": "Placeholder test reasoning.",
            "robustness": 2,
            "robustness_reasoning": "No supporting sources recorded.",
        }
    )

    assert len(state.created_page_ids) == 1
    claim_id = state.created_page_ids[0]
    links = await tmp_db.get_links_from(claim_id)
    cites_links = [l for l in links if l.link_type == LinkType.CITES]
    assert len(cites_links) == 0


async def test_multiple_cites_links(
    tmp_db,
    scout_call,
    source_page,
    second_source_page,
):
    """A claim citing multiple sources should get one CITES link per source."""
    state = MoveState(scout_call, tmp_db)
    tool = MOVES[MoveType.CREATE_CLAIM].bind(state)
    await tool.fn(
        {
            "headline": "LLM speedups concentrate at junior-analyst tasks",
            "content": "Independent estimates and firm-internal benchmarks both show LLM gains skewed toward routine junior work.",
            "credence": 6,
            "credence_reasoning": "Multiple converging sources.",
            "robustness": 3,
            "robustness_reasoning": "Two sources; a third would firm it further.",
            "source_urls": [source_page.id[:8], second_source_page.id[:8]],
        }
    )

    assert len(state.created_page_ids) == 1
    claim_id = state.created_page_ids[0]
    links = await tmp_db.get_links_from(claim_id)
    cites_links = [l for l in links if l.link_type == LinkType.CITES]
    assert len(cites_links) == 2
    cited_ids = {l.to_page_id for l in cites_links}
    assert cited_ids == {source_page.id, second_source_page.id}


async def test_cites_and_consideration_links_coexist(
    tmp_db,
    scout_call,
    question_page,
    source_page,
):
    """A claim with both source_urls and links should create both link types."""
    state = MoveState(scout_call, tmp_db)
    tool = MOVES[MoveType.CREATE_CLAIM].bind(state)
    await tool.fn(
        {
            "headline": "Sourced and linked claim",
            "content": "This claim cites a source and bears on a question.",
            "credence": 6,
            "credence_reasoning": "Source-backed assertion.",
            "robustness": 3,
            "robustness_reasoning": "One source plus a linking question.",
            "source_urls": [source_page.id[:8]],
            "links": [
                {
                    "question_id": question_page.id[:8],
                    "strength": 3.5,
                    "reasoning": "Bears on the question",
                }
            ],
        }
    )

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
    consideration_links = [l for l in question_links if l.link_type == LinkType.CONSIDERATION]
    assert len(consideration_links) >= 1, (
        "Ingest should create at least one consideration linked to the question"
    )
