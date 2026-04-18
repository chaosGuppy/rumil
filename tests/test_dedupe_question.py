"""Integration tests for the CREATE_QUESTION dedupe pipeline.

Exercises the full tool-call path: embedding search + Sonnet filter + Opus
decision. Uses real Voyage embeddings and real LLM calls (Sonnet for filter,
Haiku in test mode for the "Opus" decision).
"""

import pytest
import pytest_asyncio

from rumil.embeddings import embed_and_store_page
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
from rumil.moves.base import CreatePagePayload
from rumil.moves.create_question import execute_scout_question

pytestmark = pytest.mark.llm


async def _mk_question(db, headline: str, content: str) -> Page:
    page = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=content,
        headline=headline,
    )
    await db.save_page(page)
    await embed_and_store_page(db, page, field_name="abstract")
    return page


@pytest_asyncio.fixture
async def parent_question(tmp_db):
    return await _mk_question(
        tmp_db,
        "How quickly will frontier AI automate routine cognitive labour?",
        (
            "We want to understand the pace at which frontier AI systems "
            "will take over routine cognitive work currently performed by "
            "humans — the timeline, the shape of adoption, and the economic "
            "and organisational dynamics shaping it."
        ),
    )


@pytest_asyncio.fixture
async def scout_call(tmp_db, parent_question):
    call = Call(
        call_type=CallType.SCOUT_DEEP_QUESTIONS,
        workspace=Workspace.RESEARCH,
        scope_page_id=parent_question.id,
        status=CallStatus.PENDING,
    )
    await tmp_db.save_call(call)
    return call


def _proposed_payload() -> CreatePagePayload:
    return CreatePagePayload(
        headline="How rapidly will frontier AI automate routine cognitive work?",
        content=(
            "How fast will routine cognitive labour — the bulk of white-collar "
            "work that follows repeatable procedures — be automated by "
            "frontier AI systems? The question asks about the overall pace "
            "of displacement, not about which particular tasks go first."
        ),
        credence=5,
        robustness=1,
        workspace="research",
        supersedes=None,
        change_magnitude=None,
    )


async def test_dedupe_links_existing_duplicate(tmp_db, parent_question, scout_call):
    """A true duplicate gets linked to the parent; no new question is created."""
    duplicate = await _mk_question(
        tmp_db,
        "How fast will frontier AI automate routine cognitive labour?",
        (
            "How quickly will frontier AI systems take over routine "
            "cognitive work — the repeatable white-collar tasks that make "
            "up the bulk of current knowledge-worker jobs? We care about "
            "the overall pace of displacement."
        ),
    )
    await _mk_question(
        tmp_db,
        "Which cognitive labour tasks will frontier AI automate first?",
        (
            "Among routine cognitive labour tasks, which categories will "
            "frontier AI systems automate first? This is about the ordering "
            "of automation across task types, not the overall pace."
        ),
    )
    await _mk_question(
        tmp_db,
        "What growing conditions produce the best heirloom tomatoes?",
        (
            "Home gardeners cultivating heirloom tomatoes want to know "
            "which soil, watering, and sunlight conditions yield the best "
            "fruit."
        ),
    )

    result = await execute_scout_question(_proposed_payload(), scout_call, tmp_db)

    assert result.created_page_id == duplicate.id

    parent_links = await tmp_db.get_links_from(parent_question.id)
    child_links = [
        l
        for l in parent_links
        if l.link_type == LinkType.CHILD_QUESTION and l.to_page_id == duplicate.id
    ]
    assert len(child_links) == 1

    questions = await tmp_db.get_pages(page_type=PageType.QUESTION)
    assert len(questions) == 4


async def test_no_duplicate_creates_new_question(tmp_db, parent_question, scout_call):
    """With no real duplicate present, the proposed question is created fresh and linked."""
    await _mk_question(
        tmp_db,
        "Which cognitive labour tasks will frontier AI automate first?",
        (
            "Among routine cognitive labour tasks, which categories will "
            "frontier AI systems automate first? This is about the ordering "
            "of automation across task types."
        ),
    )
    await _mk_question(
        tmp_db,
        "What growing conditions produce the best heirloom tomatoes?",
        (
            "Home gardeners cultivating heirloom tomatoes want to know "
            "which soil, watering, and sunlight conditions yield the best "
            "fruit."
        ),
    )

    before = await tmp_db.get_pages(page_type=PageType.QUESTION)
    before_ids = {p.id for p in before}

    result = await execute_scout_question(_proposed_payload(), scout_call, tmp_db)

    assert result.created_page_id
    assert result.created_page_id not in before_ids

    new_page = await tmp_db.get_page(result.created_page_id)
    assert new_page is not None
    assert new_page.page_type == PageType.QUESTION

    parent_links = await tmp_db.get_links_from(parent_question.id)
    child_links = [
        l
        for l in parent_links
        if l.link_type == LinkType.CHILD_QUESTION and l.to_page_id == result.created_page_id
    ]
    assert len(child_links) == 1
