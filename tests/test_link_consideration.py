"""Tests for the link_consideration move's source/target validation."""

import pytest_asyncio

from rumil.models import (
    Call,
    CallStatus,
    CallType,
    LinkRole,
    LinkType,
    Page,
    PageLayer,
    PageType,
    Workspace,
)
from rumil.moves.link_consideration import (
    LinkConsiderationPayload,
    execute as execute_link_consideration,
)


@pytest_asyncio.fixture
async def claim(tmp_db):
    page = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="The sky is blue because of Rayleigh scattering.",
        headline="Rayleigh scattering explains blue sky",
    )
    await tmp_db.save_page(page)
    return page


@pytest_asyncio.fixture
async def question(tmp_db):
    page = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="Why is the sky blue?",
        headline="Why is the sky blue?",
    )
    await tmp_db.save_page(page)
    return page


@pytest_asyncio.fixture
async def judgement(tmp_db):
    page = Page(
        page_type=PageType.JUDGEMENT,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="On balance, Rayleigh scattering is the dominant mechanism.",
        headline="Rayleigh scattering is the dominant blue-sky mechanism",
    )
    await tmp_db.save_page(page)
    return page


@pytest_asyncio.fixture
async def call(tmp_db, question):
    c = Call(
        call_type=CallType.FIND_CONSIDERATIONS,
        workspace=Workspace.RESEARCH,
        scope_page_id=question.id,
        status=CallStatus.PENDING,
    )
    await tmp_db.save_call(c)
    return c


async def test_claim_to_question_creates_consideration_link(
    tmp_db, call, claim, question,
):
    payload = LinkConsiderationPayload(
        claim_id=claim.id,
        question_id=question.id,
        strength=4.0,
        reasoning="explains the mechanism",
        role=LinkRole.DIRECT,
    )
    await execute_link_consideration(payload, call, tmp_db)

    links = await tmp_db.get_links_from(claim.id)
    cons = [l for l in links if l.link_type == LinkType.CONSIDERATION]
    assert len(cons) == 1
    assert cons[0].to_page_id == question.id


async def test_judgement_source_is_rejected(
    tmp_db, call, judgement, question,
):
    payload = LinkConsiderationPayload(
        claim_id=judgement.id,
        question_id=question.id,
        strength=2.5,
        reasoning="",
        role=LinkRole.STRUCTURAL,
    )
    await execute_link_consideration(payload, call, tmp_db)

    links = await tmp_db.get_links_from(judgement.id)
    assert not [l for l in links if l.link_type == LinkType.CONSIDERATION]


async def test_non_question_target_is_rejected(
    tmp_db, call, claim,
):
    other_claim = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="Mie scattering matters too.",
        headline="Mie scattering matters too",
    )
    await tmp_db.save_page(other_claim)

    payload = LinkConsiderationPayload(
        claim_id=claim.id,
        question_id=other_claim.id,
        strength=2.5,
        reasoning="",
        role=LinkRole.STRUCTURAL,
    )
    await execute_link_consideration(payload, call, tmp_db)

    links = await tmp_db.get_links_from(claim.id)
    assert not [l for l in links if l.link_type == LinkType.CONSIDERATION]
