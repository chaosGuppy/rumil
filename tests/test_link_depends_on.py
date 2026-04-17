"""Tests for the link_depends_on move's source/target validation."""

import pytest_asyncio

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
from rumil.moves.link_depends_on import (
    LinkDependsOnPayload,
)
from rumil.moves.link_depends_on import (
    execute as execute_link_depends_on,
)


@pytest_asyncio.fixture
async def claim(tmp_db):
    page = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="The sky is blue because of Rayleigh scattering.",
        headline="Rayleigh scattering",
    )
    await tmp_db.save_page(page)
    return page


@pytest_asyncio.fixture
async def other_claim(tmp_db):
    page = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="Shorter wavelengths scatter more.",
        headline="Shorter wavelengths scatter more",
    )
    await tmp_db.save_page(page)
    return page


@pytest_asyncio.fixture
async def judgement(tmp_db):
    page = Page(
        page_type=PageType.JUDGEMENT,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="On balance, Rayleigh scattering dominates.",
        headline="Rayleigh scattering dominates",
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
async def call(tmp_db, question):
    c = Call(
        call_type=CallType.ASSESS,
        workspace=Workspace.RESEARCH,
        scope_page_id=question.id,
        status=CallStatus.PENDING,
    )
    await tmp_db.save_call(c)
    return c


async def test_claim_to_claim_dependency_is_created(
    tmp_db,
    call,
    claim,
    other_claim,
):
    payload = LinkDependsOnPayload(
        dependent_page_id=claim.id,
        dependency_page_id=other_claim.id,
        strength=4.0,
        reasoning="builds directly on the wavelength claim",
    )
    await execute_link_depends_on(payload, call, tmp_db)

    links = await tmp_db.get_links_from(claim.id)
    deps = [l for l in links if l.link_type == LinkType.DEPENDS_ON]
    assert len(deps) == 1
    assert deps[0].to_page_id == other_claim.id


async def test_judgement_to_claim_dependency_is_created(
    tmp_db,
    call,
    judgement,
    claim,
):
    payload = LinkDependsOnPayload(
        dependent_page_id=judgement.id,
        dependency_page_id=claim.id,
        strength=3.0,
        reasoning="",
    )
    await execute_link_depends_on(payload, call, tmp_db)

    links = await tmp_db.get_links_from(judgement.id)
    deps = [l for l in links if l.link_type == LinkType.DEPENDS_ON]
    assert len(deps) == 1
    assert deps[0].to_page_id == claim.id


async def test_question_dependent_is_rejected(
    tmp_db,
    call,
    question,
    claim,
):
    """A question can't depend on anything via depends_on; use child_question instead."""
    payload = LinkDependsOnPayload(
        dependent_page_id=question.id,
        dependency_page_id=claim.id,
        strength=3.0,
        reasoning="",
    )
    await execute_link_depends_on(payload, call, tmp_db)

    links = await tmp_db.get_links_from(question.id)
    assert not [l for l in links if l.link_type == LinkType.DEPENDS_ON]


async def test_question_dependency_is_rejected(
    tmp_db,
    call,
    claim,
    question,
):
    """Depending on a question (rather than its judgement) is forbidden."""
    payload = LinkDependsOnPayload(
        dependent_page_id=claim.id,
        dependency_page_id=question.id,
        strength=3.0,
        reasoning="",
    )
    result = await execute_link_depends_on(payload, call, tmp_db)

    links = await tmp_db.get_links_from(claim.id)
    assert not [l for l in links if l.link_type == LinkType.DEPENDS_ON]
    assert "judgement" in result.message.lower()
