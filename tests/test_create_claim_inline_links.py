"""Tests for create_claim's inline consideration link target validation."""

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
from rumil.moves.create_claim import CreateClaimPayload
from rumil.moves.create_claim import execute as execute_create_claim
from rumil.moves.link_consideration import ConsiderationLinkFields


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
async def call(tmp_db, question):
    c = Call(
        call_type=CallType.FIND_CONSIDERATIONS,
        workspace=Workspace.RESEARCH,
        scope_page_id=question.id,
        status=CallStatus.PENDING,
    )
    await tmp_db.save_call(c)
    return c


async def test_inline_link_to_question_creates_consideration(tmp_db, call, question):
    payload = CreateClaimPayload(
        content="Rayleigh scattering explains the blue color.",
        headline="Rayleigh scattering",
        credence=6,
        robustness=3,
        workspace="research",
        supersedes=None,
        change_magnitude=None,
        links=[
            ConsiderationLinkFields(
                question_id=question.id,
                strength=4.0,
                reasoning="explains the mechanism",
                role=LinkRole.DIRECT,
            )
        ],
    )
    result = await execute_create_claim(payload, call, tmp_db)

    assert result.created_page_id
    links = await tmp_db.get_links_from(result.created_page_id)
    cons = [l for l in links if l.link_type == LinkType.CONSIDERATION]
    assert len(cons) == 1
    assert cons[0].to_page_id == question.id


async def test_inline_link_to_non_question_target_is_skipped(tmp_db, call):
    """If the resolved target is not a question, no CONSIDERATION link is created."""
    other_claim = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="Mie scattering matters too.",
        headline="Mie scattering matters too",
    )
    await tmp_db.save_page(other_claim)

    payload = CreateClaimPayload(
        content="Rayleigh scattering explains the blue color.",
        headline="Rayleigh scattering",
        credence=6,
        robustness=3,
        workspace="research",
        supersedes=None,
        change_magnitude=None,
        links=[
            ConsiderationLinkFields(
                question_id=other_claim.id,
                strength=4.0,
                reasoning="should not be allowed",
                role=LinkRole.STRUCTURAL,
            )
        ],
    )
    result = await execute_create_claim(payload, call, tmp_db)

    assert result.created_page_id
    links = await tmp_db.get_links_from(result.created_page_id)
    assert not [l for l in links if l.link_type == LinkType.CONSIDERATION]
