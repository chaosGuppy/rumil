"""End-to-end test for the web research call type."""

import pytest
import pytest_asyncio

from rumil.calls.web_research import WebResearchCall
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


@pytest_asyncio.fixture
async def web_question(tmp_db):
    """A factual question whose answer is past Claude's training cutoff."""
    page = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="Who won the 2025 Nobel Prize in Physics?",
        headline="Who won the 2025 Nobel Prize in Physics?",
    )
    await tmp_db.save_page(page)
    return page


@pytest_asyncio.fixture
async def web_research_call(tmp_db, web_question):
    call = Call(
        call_type=CallType.WEB_RESEARCH,
        workspace=Workspace.RESEARCH,
        scope_page_id=web_question.id,
        status=CallStatus.PENDING,
    )
    await tmp_db.save_call(call)
    return call


@pytest.mark.integration
async def test_web_research_creates_sourced_claims(
    tmp_db, web_question, web_research_call,
):
    """Web research call finds web sources, creates claims with CITES links."""
    wrc = WebResearchCall(
        web_question.id, web_research_call, tmp_db,
    )
    await wrc.run()

    refreshed = await tmp_db.get_call(web_research_call.id)
    assert refreshed.status == CallStatus.COMPLETE
    assert refreshed.completed_at is not None
    assert 'Web research complete' in refreshed.result_summary

    created_ids = wrc.infra.state.created_page_ids
    assert len(created_ids) >= 1, 'Expected at least one page created'

    claim_ids = []
    source_ids = []
    for pid in created_ids:
        page = await tmp_db.get_page(pid)
        assert page is not None
        if page.page_type == PageType.CLAIM:
            claim_ids.append(pid)
        elif page.page_type == PageType.SOURCE:
            source_ids.append(pid)

    assert len(claim_ids) >= 1, 'Expected at least one claim'
    assert len(source_ids) >= 1, 'Expected at least one source page'

    source_page = await tmp_db.get_page(source_ids[0])
    assert source_page.extra.get('url', '').startswith('http')
    assert source_page.extra.get('fetched_at') is not None
    assert len(source_page.content) > 0

    cites_links = []
    for cid in claim_ids:
        links = await tmp_db.get_links_from(cid)
        for link in links:
            if link.link_type == LinkType.CITES:
                cites_links.append(link)
    assert len(cites_links) >= 1, 'Expected at least one CITES link'

    cites_targets = {link.to_page_id for link in cites_links}
    assert cites_targets & set(source_ids), (
        'At least one CITES link should point to a created source page'
    )

    trace = await tmp_db.get_call_trace(web_research_call.id)
    event_types = [e.get('event') for e in trace]
    assert 'context_built' in event_types
