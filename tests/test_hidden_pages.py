"""Regression tests for the `hidden` page flag and the `include_hidden`
parameter threaded through discovery DB helpers.

Contract under test:
- By-ID fetches (``get_page``, ``get_pages_by_ids``) return pages
  regardless of ``hidden``.
- Discovery helpers default to excluding hidden pages; passing
  ``include_hidden=True`` surfaces them.
- The hidden filter composes correctly with staged-run visibility.
"""

import uuid

import pytest
import pytest_asyncio

from rumil.database import DB
from rumil.embeddings import embed_and_store_page, embed_query, search_pages_by_vector
from rumil.models import (
    LinkType,
    Page,
    PageLayer,
    PageLink,
    PageType,
    Workspace,
)


async def _make_page(
    db: DB,
    headline: str,
    *,
    page_type: PageType = PageType.CLAIM,
    hidden: bool = False,
) -> Page:
    page = Page(
        page_type=page_type,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=f"Content for: {headline}",
        headline=headline,
        hidden=hidden,
    )
    await db.save_page(page)
    return page


async def test_get_page_returns_hidden(tmp_db):
    """By-id fetch returns a hidden page — `hidden` only filters discovery."""
    page = await _make_page(tmp_db, "direct-fetch hidden", hidden=True)
    fetched = await tmp_db.get_page(page.id)
    assert fetched is not None
    assert fetched.id == page.id
    assert fetched.hidden is True


async def test_get_pages_by_ids_returns_hidden(tmp_db):
    """Batched by-id fetch returns hidden pages unchanged."""
    hidden = await _make_page(tmp_db, "batch hidden", hidden=True)
    visible = await _make_page(tmp_db, "batch visible", hidden=False)
    result = await tmp_db.get_pages_by_ids([hidden.id, visible.id])
    assert hidden.id in result and visible.id in result
    assert result[hidden.id].hidden is True
    assert result[visible.id].hidden is False


async def test_get_pages_filters_hidden_by_default(tmp_db):
    """get_pages excludes hidden pages unless include_hidden=True."""
    visible = await _make_page(tmp_db, "visible claim")
    hidden = await _make_page(tmp_db, "hidden claim", hidden=True)

    default_ids = {p.id for p in await tmp_db.get_pages(page_type=PageType.CLAIM)}
    assert visible.id in default_ids
    assert hidden.id not in default_ids

    all_ids = {
        p.id
        for p in await tmp_db.get_pages(
            page_type=PageType.CLAIM,
            include_hidden=True,
        )
    }
    assert hidden.id in all_ids


async def test_get_pages_slim_filters_hidden_by_default(tmp_db):
    visible = await _make_page(tmp_db, "slim visible")
    hidden = await _make_page(tmp_db, "slim hidden", hidden=True)
    default_ids = {p.id for p in await tmp_db.get_pages_slim()}
    assert visible.id in default_ids
    assert hidden.id not in default_ids
    all_ids = {p.id for p in await tmp_db.get_pages_slim(include_hidden=True)}
    assert hidden.id in all_ids


async def test_get_pages_paginated_filters_hidden_by_default(tmp_db):
    visible = await _make_page(tmp_db, "paginated visible")
    hidden = await _make_page(tmp_db, "paginated hidden", hidden=True)
    pages, _ = await tmp_db.get_pages_paginated()
    default_ids = {p.id for p in pages}
    assert visible.id in default_ids
    assert hidden.id not in default_ids
    pages_all, _ = await tmp_db.get_pages_paginated(include_hidden=True)
    assert hidden.id in {p.id for p in pages_all}


async def test_get_root_questions_filters_hidden_by_default(tmp_db):
    """Hidden root questions are invisible by default, visible with the flag."""
    visible_q = await _make_page(
        tmp_db,
        "visible root",
        page_type=PageType.QUESTION,
    )
    hidden_q = await _make_page(
        tmp_db,
        "hidden root",
        page_type=PageType.QUESTION,
        hidden=True,
    )
    default_ids = {p.id for p in await tmp_db.get_root_questions()}
    assert visible_q.id in default_ids
    assert hidden_q.id not in default_ids

    all_ids = {p.id for p in await tmp_db.get_root_questions(include_hidden=True)}
    assert hidden_q.id in all_ids


async def test_get_child_questions_filters_hidden_by_default(tmp_db):
    parent = await _make_page(tmp_db, "parent q", page_type=PageType.QUESTION)
    visible_child = await _make_page(tmp_db, "visible child", page_type=PageType.QUESTION)
    hidden_child = await _make_page(
        tmp_db, "hidden child", page_type=PageType.QUESTION, hidden=True
    )
    for child in (visible_child, hidden_child):
        await tmp_db.save_link(
            PageLink(
                from_page_id=parent.id,
                to_page_id=child.id,
                link_type=LinkType.CHILD_QUESTION,
            )
        )

    default_ids = {p.id for p in await tmp_db.get_child_questions(parent.id)}
    assert visible_child.id in default_ids
    assert hidden_child.id not in default_ids

    all_ids = {p.id for p in await tmp_db.get_child_questions(parent.id, include_hidden=True)}
    assert hidden_child.id in all_ids


async def test_get_considerations_filters_hidden_by_default(tmp_db):
    question = await _make_page(tmp_db, "q for considerations", page_type=PageType.QUESTION)
    visible_claim = await _make_page(tmp_db, "visible consideration")
    hidden_claim = await _make_page(tmp_db, "hidden consideration", hidden=True)
    for claim in (visible_claim, hidden_claim):
        await tmp_db.save_link(
            PageLink(
                from_page_id=claim.id,
                to_page_id=question.id,
                link_type=LinkType.CONSIDERATION,
                strength=3.0,
                reasoning="t",
            )
        )

    default_ids = {p.id for p, _ in await tmp_db.get_considerations_for_question(question.id)}
    assert visible_claim.id in default_ids
    assert hidden_claim.id not in default_ids

    all_ids = {
        p.id
        for p, _ in await tmp_db.get_considerations_for_question(question.id, include_hidden=True)
    }
    assert hidden_claim.id in all_ids


async def test_get_judgements_filters_hidden_by_default(tmp_db):
    question = await _make_page(tmp_db, "q for judgements", page_type=PageType.QUESTION)
    visible_j = await _make_page(tmp_db, "visible judgement", page_type=PageType.JUDGEMENT)
    hidden_j = await _make_page(
        tmp_db, "hidden judgement", page_type=PageType.JUDGEMENT, hidden=True
    )
    for j in (visible_j, hidden_j):
        await tmp_db.save_link(
            PageLink(
                from_page_id=j.id,
                to_page_id=question.id,
                link_type=LinkType.ANSWERS,
            )
        )

    default_ids = {p.id for p in await tmp_db.get_judgements_for_question(question.id)}
    assert visible_j.id in default_ids
    assert hidden_j.id not in default_ids

    all_ids = {
        p.id for p in await tmp_db.get_judgements_for_question(question.id, include_hidden=True)
    }
    assert hidden_j.id in all_ids


@pytest_asyncio.fixture
async def staged_project():
    """Shared project for testing staged vs baseline hidden-page visibility."""
    setup_db = await DB.create(run_id=str(uuid.uuid4()))
    project = await setup_db.get_or_create_project(f"test-hidden-staged-{uuid.uuid4().hex[:8]}")
    yield project.id
    await setup_db._execute(setup_db.client.table("projects").delete().eq("id", project.id))


async def test_hidden_filter_composes_with_staged_runs(staged_project):
    """A staged run's hidden page is invisible to non-staged observers, and
    invisible by default even to the staged reader until include_hidden=True."""
    staged = await DB.create(run_id=str(uuid.uuid4()), staged=True)
    staged.project_id = staged_project
    await staged.init_budget(10)

    observer = await DB.create(run_id=str(uuid.uuid4()), staged=False)
    observer.project_id = staged_project

    try:
        hidden = await _make_page(staged, "staged hidden", page_type=PageType.QUESTION, hidden=True)

        observer_ids = {p.id for p in await observer.get_root_questions()}
        assert hidden.id not in observer_ids
        observer_ids_all = {p.id for p in await observer.get_root_questions(include_hidden=True)}
        assert hidden.id not in observer_ids_all

        staged_default = {p.id for p in await staged.get_root_questions()}
        assert hidden.id not in staged_default
        staged_all = {p.id for p in await staged.get_root_questions(include_hidden=True)}
        assert hidden.id in staged_all
    finally:
        await staged.delete_run_data()
        await observer.close()


async def test_baseline_hidden_visible_to_staged_reader_with_flag(staged_project):
    """A baseline (non-staged) hidden page is visible to a staged reader when
    include_hidden=True, and filtered out by default — confirming the hidden
    filter and the staged visibility filter compose in both directions."""
    baseline = await DB.create(run_id=str(uuid.uuid4()), staged=False)
    baseline.project_id = staged_project
    await baseline.init_budget(10)

    staged_reader = await DB.create(run_id=str(uuid.uuid4()), staged=True)
    staged_reader.project_id = staged_project

    try:
        hidden = await _make_page(
            baseline, "baseline hidden", page_type=PageType.QUESTION, hidden=True
        )

        staged_default = {p.id for p in await staged_reader.get_root_questions()}
        assert hidden.id not in staged_default

        staged_all = {p.id for p in await staged_reader.get_root_questions(include_hidden=True)}
        assert hidden.id in staged_all
    finally:
        await baseline.delete_run_data()
        await staged_reader.close()


@pytest.mark.llm
async def test_search_pages_by_vector_filters_hidden_by_default(tmp_db):
    """Embedding search excludes hidden pages by default; include_hidden surfaces them."""
    visible = await _make_page(
        tmp_db,
        "Photosynthesis converts sunlight into chemical energy in plants.",
    )
    hidden = await _make_page(
        tmp_db,
        "Photosynthesis underpins food chains by transforming light into sugars.",
        hidden=True,
    )
    await embed_and_store_page(tmp_db, visible)
    await embed_and_store_page(tmp_db, hidden)

    query_vec = await embed_query("how do plants convert light into energy?")

    default_ids = {
        p.id
        for p, _ in await search_pages_by_vector(
            tmp_db, query_vec, match_threshold=0.2, match_count=10
        )
    }
    assert visible.id in default_ids
    assert hidden.id not in default_ids

    all_ids = {
        p.id
        for p, _ in await search_pages_by_vector(
            tmp_db,
            query_vec,
            match_threshold=0.2,
            match_count=10,
            include_hidden=True,
        )
    }
    assert hidden.id in all_ids
