"""Tests for build_embedding_based_context."""

import pytest

from rumil.context import (
    EmbeddingBasedContextResult,
    build_embedding_based_context,
    format_page,
)
from rumil.models import Page, PageDetail, PageLayer, PageType, Workspace


def _make_page(headline: str, content: str, page_type: PageType = PageType.CLAIM) -> Page:
    return Page(
        page_type=page_type,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=content,
        headline=headline,
        epistemic_status=3.0,
        epistemic_type='estimate',
    )


PAGES = [
    _make_page('Alpha finding', 'A' * 200),
    _make_page('Beta finding', 'B' * 200),
    _make_page('Gamma finding', 'C' * 200),
    _make_page('Delta finding', 'D' * 200),
    _make_page('Epsilon finding', 'E' * 200),
]

RANKED = [(page, 0.9 - i * 0.1) for i, page in enumerate(PAGES)]

FAKE_EMBEDDING = [0.1] * 1024


@pytest.fixture
def mock_embeddings(mocker):
    mocker.patch(
        'rumil.context.embed_query',
        new_callable=mocker.AsyncMock,
        return_value=FAKE_EMBEDDING,
    )
    mocker.patch(
        'rumil.context.search_pages_by_vector',
        new_callable=mocker.AsyncMock,
        return_value=RANKED,
    )


@pytest.fixture
def mock_db(mocker):
    db = mocker.AsyncMock()
    db.get_considerations_for_question = mocker.AsyncMock(return_value=[])
    db.get_judgements_for_question = mocker.AsyncMock(return_value=[])
    return db


async def test_basic_budget_split(mock_embeddings, mock_db):
    """Pages fill full tier first, then summary tier."""
    result = await build_embedding_based_context(
        'test query',
        mock_db,
        full_page_char_budget=6_000,
        summary_page_char_budget=3_000,
    )

    assert isinstance(result, EmbeddingBasedContextResult)
    assert len(result.full_page_ids) > 0
    assert result.budget_usage['full'] <= 6_000
    assert result.budget_usage['summary'] <= 3_000
    assert result.distillation_page_ids == []
    assert set(result.page_ids) == set(
        result.full_page_ids + result.abstract_page_ids + result.summary_page_ids
    )

async def test_similarity_ordering(mock_embeddings, mock_db):
    """Pages appear in similarity order: highest-similarity first in full tier."""
    result = await build_embedding_based_context(
        'test query',
        mock_db,
        full_page_char_budget=10_000,
    )

    if len(result.full_page_ids) >= 2:
        full_ids = result.full_page_ids
        page_order = [p.id for p, _ in RANKED]
        for i in range(len(full_ids) - 1):
            assert page_order.index(full_ids[i]) < page_order.index(full_ids[i + 1])


async def test_small_budget_forces_lower_tiers(mock_embeddings, mock_db):
    """With a tiny full budget, pages overflow to abstract/summary tiers."""
    result = await build_embedding_based_context(
        'test query',
        mock_db,
        full_page_char_budget=100,
        abstract_page_char_budget=800,
        summary_page_char_budget=800,
    )

    assert len(result.abstract_page_ids) + len(result.summary_page_ids) > 0


async def test_section_headers_present(mock_embeddings, mock_db):
    """Output contains expected section headers."""
    result = await build_embedding_based_context(
        'test query',
        mock_db,
        full_page_char_budget=10_000,
    )

    assert '## Relevant Pages (Full)' in result.context_text
    if result.summary_page_ids:
        assert '## Relevant Pages (Summaries)' in result.context_text


async def test_format_page_headline():
    """format_page with HEADLINE detail produces the expected compact line."""
    page = _make_page('Test summary', 'content')
    line = await format_page(page, PageDetail.HEADLINE)
    assert '[CLAIM 3/5]' in line
    assert page.id[:8] in line
    assert 'Test summary' in line


async def test_zero_budget_returns_empty(mock_embeddings, mock_db):
    """A zero budget produces no pages."""
    result = await build_embedding_based_context(
        'test query',
        mock_db,
        full_page_char_budget=0,
        abstract_page_char_budget=0,
        summary_page_char_budget=0,
        distillation_page_char_budget=0,
    )
    assert result.page_ids == []
    assert result.full_page_ids == []
    assert result.summary_page_ids == []
