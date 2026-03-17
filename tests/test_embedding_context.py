"""Tests for build_embedding_based_context."""

from unittest.mock import AsyncMock, patch

import pytest

from rumil.context import (
    EmbeddingBasedContextResult,
    _format_page_summary,
    build_embedding_based_context,
)
from rumil.models import Page, PageLayer, PageType, Workspace


def _make_page(summary: str, content: str, page_type: PageType = PageType.CLAIM) -> Page:
    return Page(
        page_type=page_type,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=content,
        summary=summary,
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
def mock_embeddings():
    with (
        patch(
            'rumil.context.embed_query',
            new_callable=AsyncMock,
            return_value=FAKE_EMBEDDING,
        ) as mock_eq,
        patch(
            'rumil.context.search_pages_by_vector',
            new_callable=AsyncMock,
            return_value=RANKED,
        ) as mock_sp,
    ):
        yield mock_eq, mock_sp


async def test_basic_budget_split(mock_embeddings):
    """Pages fill full tier first, then summary tier."""
    mock_eq, mock_sp = mock_embeddings
    db = AsyncMock()
    db.get_considerations_for_question = AsyncMock(return_value=[])
    db.get_judgements_for_question = AsyncMock(return_value=[])

    result = await build_embedding_based_context(
        'test query',
        db,
        context_char_budget=10_000,
        full_page_char_fraction=0.6,
        summary_para_char_fraction=0.3,
    )

    assert isinstance(result, EmbeddingBasedContextResult)
    assert len(result.full_page_ids) > 0
    assert result.budget_usage['full'] <= 6_000
    assert result.budget_usage['summary'] <= 3_000
    assert result.distillation_page_ids == []
    assert set(result.page_ids) == set(result.full_page_ids + result.summary_page_ids)

    mock_eq.assert_awaited_once_with('test query')
    mock_sp.assert_awaited_once()
    call_kwargs = mock_sp.call_args
    assert call_kwargs.kwargs['field_name'] == 'summary'
    assert call_kwargs.kwargs['match_count'] == 500


async def test_similarity_ordering(mock_embeddings):
    """Pages appear in similarity order: highest-similarity first in full tier."""
    mock_embeddings  # just activate the fixture
    db = AsyncMock()
    db.get_considerations_for_question = AsyncMock(return_value=[])
    db.get_judgements_for_question = AsyncMock(return_value=[])

    result = await build_embedding_based_context(
        'test query',
        db,
        context_char_budget=10_000,
    )

    if len(result.full_page_ids) >= 2:
        full_ids = result.full_page_ids
        page_order = [p.id for p, _ in RANKED]
        for i in range(len(full_ids) - 1):
            assert page_order.index(full_ids[i]) < page_order.index(full_ids[i + 1])


async def test_small_budget_forces_summaries(mock_embeddings):
    """With a tiny full budget, pages overflow to summary tier."""
    mock_embeddings
    db = AsyncMock()
    db.get_considerations_for_question = AsyncMock(return_value=[])
    db.get_judgements_for_question = AsyncMock(return_value=[])

    result = await build_embedding_based_context(
        'test query',
        db,
        context_char_budget=1_000,
        full_page_char_fraction=0.1,
        summary_para_char_fraction=0.8,
    )

    assert len(result.summary_page_ids) > 0


async def test_section_headers_present(mock_embeddings):
    """Output contains expected section headers."""
    mock_embeddings
    db = AsyncMock()
    db.get_considerations_for_question = AsyncMock(return_value=[])
    db.get_judgements_for_question = AsyncMock(return_value=[])

    result = await build_embedding_based_context(
        'test query',
        db,
        context_char_budget=10_000,
    )

    assert '## Relevant Pages (Full)' in result.context_text
    if result.summary_page_ids:
        assert '## Relevant Pages (Summaries)' in result.context_text


def test_format_page_summary():
    """_format_page_summary produces the expected compact line."""
    page = _make_page('Test summary', 'content')
    line = _format_page_summary(page)
    assert '[CLAIM 3/5]' in line
    assert page.id[:8] in line
    assert 'Test summary' in line


async def test_zero_budget_returns_empty():
    """A zero budget produces no pages."""
    with (
        patch(
            'rumil.context.embed_query',
            new_callable=AsyncMock,
            return_value=FAKE_EMBEDDING,
        ),
        patch(
            'rumil.context.search_pages_by_vector',
            new_callable=AsyncMock,
            return_value=RANKED,
        ),
    ):
        db = AsyncMock()
        result = await build_embedding_based_context(
            'test query',
            db,
            context_char_budget=0,
        )
        assert result.page_ids == []
        assert result.full_page_ids == []
        assert result.summary_page_ids == []
