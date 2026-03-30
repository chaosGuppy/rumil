"""Tests for inline citation extraction and auto-linking."""

import re

import pytest
import pytest_asyncio

from rumil.models import (
    Call,
    CallStatus,
    CallType,
    ConsiderationDirection,
    LinkType,
    Page,
    PageLayer,
    PageLink,
    PageType,
    Workspace,
)
from rumil.moves.base import _CITATION_RE, extract_and_link_citations


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
async def claim_page(tmp_db):
    page = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="Shorter wavelengths scatter more, making the sky blue.",
        headline="Short-wavelength scattering produces blue sky",
    )
    await tmp_db.save_page(page)
    return page


@pytest_asyncio.fixture
async def second_claim_page(tmp_db):
    page = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="Sunset colors result from longer path lengths through the atmosphere.",
        headline="Longer atmospheric path lengths produce sunset colors",
    )
    await tmp_db.save_page(page)
    return page


async def test_inline_citation_of_source_creates_cites_link(
    tmp_db, source_page,
):
    """An inline [shortid] citing a SOURCE page should produce a CITES link."""
    citing_page = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=f"According to [{source_page.id[:8]}], scattering causes blue sky.",
        headline="Scattering causes blue sky",
    )
    await tmp_db.save_page(citing_page)

    linked = await extract_and_link_citations(citing_page.id, citing_page.content, tmp_db)

    assert linked == {source_page.id}
    links = await tmp_db.get_links_from(citing_page.id)
    cites = [l for l in links if l.link_type == LinkType.CITES]
    assert len(cites) == 1
    assert cites[0].to_page_id == source_page.id


async def test_inline_citation_of_claim_creates_consideration_link(
    tmp_db, claim_page,
):
    """An inline [shortid] citing a CLAIM from a CLAIM should produce a CONSIDERATION link."""
    citing_page = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=f"Building on [{claim_page.id[:8]}], the effect is strongest at noon.",
        headline="Scattering effect strongest at noon",
    )
    await tmp_db.save_page(citing_page)

    linked = await extract_and_link_citations(
        citing_page.id, citing_page.content, tmp_db,

    )

    assert linked == {claim_page.id}
    consideration_links = await tmp_db.get_links_to(citing_page.id)
    cons = [l for l in consideration_links if l.link_type == LinkType.CONSIDERATION]
    assert len(cons) == 1
    assert cons[0].from_page_id == claim_page.id
    assert cons[0].to_page_id == citing_page.id


async def test_multiple_inline_citations(
    tmp_db, claim_page, second_claim_page,
):
    """Content with multiple [shortid] citations creates one CONSIDERATION link per cited claim."""
    citing_page = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=(
            f"Combining [{claim_page.id[:8]}] and [{second_claim_page.id[:8]}], "
            "we see a coherent picture of atmospheric optics."
        ),
        headline="Atmospheric optics coherent picture",
    )
    await tmp_db.save_page(citing_page)

    linked = await extract_and_link_citations(
        citing_page.id, citing_page.content, tmp_db,

    )

    assert linked == {claim_page.id, second_claim_page.id}
    consideration_links = await tmp_db.get_links_to(citing_page.id)
    cons = [l for l in consideration_links if l.link_type == LinkType.CONSIDERATION]
    assert len(cons) == 2
    assert {l.from_page_id for l in cons} == {claim_page.id, second_claim_page.id}


async def test_judgement_citing_claim_creates_consideration_link(
    tmp_db, claim_page,
):
    """A JUDGEMENT citing a CLAIM should produce a CONSIDERATION link (claim → judgement)."""
    judgement = Page(
        page_type=PageType.JUDGEMENT,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=f"Weighing [{claim_page.id[:8]}], the evidence is strong.",
        headline="Evidence for blue sky is strong",
    )
    await tmp_db.save_page(judgement)

    linked = await extract_and_link_citations(
        judgement.id, judgement.content, tmp_db,
    )

    assert linked == {claim_page.id}
    consideration_links = await tmp_db.get_links_to(judgement.id)
    cons = [l for l in consideration_links if l.link_type == LinkType.CONSIDERATION]
    assert len(cons) == 1
    assert cons[0].from_page_id == claim_page.id
    assert cons[0].to_page_id == judgement.id


async def test_question_citing_claim_creates_consideration_link(
    tmp_db, claim_page,
):
    """A QUESTION citing a CLAIM should produce a CONSIDERATION link (claim → question)."""
    question = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=f"Given [{claim_page.id[:8]}], what are the implications for sunset colors?",
        headline="What are the implications for sunset colors?",
    )
    await tmp_db.save_page(question)

    linked = await extract_and_link_citations(
        question.id, question.content, tmp_db,
    )

    assert linked == {claim_page.id}
    consideration_links = await tmp_db.get_links_to(question.id)
    cons = [l for l in consideration_links if l.link_type == LinkType.CONSIDERATION]
    assert len(cons) == 1
    assert cons[0].from_page_id == claim_page.id
    assert cons[0].to_page_id == question.id


async def test_citing_question_creates_related_link_cited_to_citing(tmp_db):
    """Citing a QUESTION should produce a RELATED link from the cited question to the citing page."""
    question = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="Why is the sky blue?",
        headline="Why is the sky blue?",
    )
    await tmp_db.save_page(question)

    judgement = Page(
        page_type=PageType.JUDGEMENT,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=f"This relates to [{question.id[:8]}] but approaches it differently.",
        headline="Alternative approach to sky color",
    )
    await tmp_db.save_page(judgement)

    linked = await extract_and_link_citations(
        judgement.id, judgement.content, tmp_db,
    )

    assert linked == {question.id}
    links_to_judgement = await tmp_db.get_links_to(judgement.id)
    related = [l for l in links_to_judgement if l.link_type == LinkType.RELATED]
    assert len(related) == 1
    assert related[0].from_page_id == question.id
    assert related[0].to_page_id == judgement.id


async def test_unresolvable_short_ids_skipped(tmp_db):
    """Citations referencing nonexistent pages are silently skipped."""
    page = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="This references [deadbeef] which does not exist.",
        headline="References nonexistent page",
    )
    await tmp_db.save_page(page)

    linked = await extract_and_link_citations(page.id, page.content, tmp_db)

    assert linked == set()
    links = await tmp_db.get_links_from(page.id)
    assert len(links) == 0


def test_regex_ignores_non_hex_and_wrong_length():
    """The citation regex should only match exactly 8 lowercase hex chars in brackets."""
    assert _CITATION_RE.findall("[abcdef01]") == ["abcdef01"]
    assert _CITATION_RE.findall("[12345678]") == ["12345678"]

    assert _CITATION_RE.findall("[abc]") == []
    assert _CITATION_RE.findall("[not-hex!]") == []
    assert _CITATION_RE.findall("[ABCDEF01]") == []
    assert _CITATION_RE.findall("[abcdef012]") == []
    assert _CITATION_RE.findall("[link text](http://example.com)") == []


async def test_page_does_not_cite_itself(tmp_db):
    """A page whose content contains its own short ID should not self-link."""
    page = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="placeholder",
        headline="Self-referencing page",
    )
    await tmp_db.save_page(page)

    content_with_self = f"As noted earlier in [{page.id[:8]}], this is recursive."
    linked = await extract_and_link_citations(page.id, content_with_self, tmp_db)

    assert linked == set()
    links = await tmp_db.get_links_from(page.id)
    assert len(links) == 0


@pytest.mark.llm
async def test_assess_produces_inline_citations(tmp_db, question_page):
    """An assess call should produce a judgement with inline citations that get auto-linked."""
    claims = []
    for headline, content, direction in [
        (
            "Rayleigh scattering is the dominant blue-sky mechanism",
            "Rayleigh scattering of shorter wavelengths of light by molecules "
            "in Earth's atmosphere is the dominant mechanism for blue sky.",
            "supports",
        ),
        (
            "Mie scattering from aerosols shifts sky toward white or grey",
            "In polluted or humid conditions, larger particles cause Mie scattering "
            "which is wavelength-independent, shifting the sky toward white or grey.",
            "opposes",
        ),
        (
            "Human color perception adapts to ambient lighting conditions",
            "Chromatic adaptation in the human visual system means the perceived "
            "color of the sky depends on adaptation state, not just the spectrum.",
            "neutral",
        ),
    ]:
        claim = Page(
            page_type=PageType.CLAIM,
            layer=PageLayer.SQUIDGY,
            workspace=Workspace.RESEARCH,
            content=content,
            headline=headline,
            credence=6,
            robustness=3,
        )
        await tmp_db.save_page(claim)
        await tmp_db.save_link(PageLink(
            from_page_id=claim.id,
            to_page_id=question_page.id,
            link_type=LinkType.CONSIDERATION,
            strength=3.5,
            reasoning=headline,
            direction=ConsiderationDirection(direction),
        ))
        claims.append(claim)

    call = Call(
        call_type=CallType.ASSESS,
        workspace=Workspace.RESEARCH,
        scope_page_id=question_page.id,
        status=CallStatus.PENDING,
    )
    await tmp_db.save_call(call)

    from rumil.calls.assess import AssessCall

    runner = AssessCall(question_page.id, call, tmp_db)
    await runner.run()

    refreshed = await tmp_db.get_call(call.id)
    assert refreshed.status == CallStatus.COMPLETE

    citation_pattern = re.compile(r'\[([a-f0-9]{8})\]')

    created_pages = []
    for pid in runner.infra.state.created_page_ids:
        p = await tmp_db.get_page(pid)
        if p:
            created_pages.append(p)

    pages_with_citations = [
        p for p in created_pages
        if citation_pattern.search(p.content)
    ]
    assert len(pages_with_citations) >= 1, (
        "At least one created page should contain an inline [shortid] citation"
    )

    for p in pages_with_citations:
        cited_short_ids = citation_pattern.findall(p.content)
        links_from = await tmp_db.get_links_from(p.id)
        links_to = await tmp_db.get_links_to(p.id)
        linked_ids = (
            {l.to_page_id for l in links_from}
            | {l.from_page_id for l in links_to}
        )
        for sid in cited_short_ids:
            resolved = await tmp_db.resolve_page_id(sid)
            if resolved:
                assert resolved in linked_ids, (
                    f"Citation [{sid}] in page {p.id[:8]} should have a corresponding link"
                )
