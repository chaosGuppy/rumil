"""Tests for embedding creation and vector search."""

import pytest

from rumil.embeddings import (
    EMBEDDING_DIMENSIONS,
    embed_and_store_page,
    embed_texts,
    search_pages,
)
from rumil.models import Page, PageLayer, PageType, Workspace


pytestmark = pytest.mark.llm


async def test_embed_texts_returns_correct_dimensions():
    """embed_texts returns one vector per input with the expected dimension."""
    vectors = await embed_texts(["alpha", "beta"])
    assert len(vectors) == 2
    assert all(len(v) == EMBEDDING_DIMENSIONS for v in vectors)


async def test_embed_texts_empty_input():
    """embed_texts returns an empty list for empty input."""
    vectors = await embed_texts([])
    assert vectors == []


async def test_store_and_search_round_trip(tmp_db):
    """A page that has been embedded can be retrieved by a similar query."""
    page = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="Photosynthesis converts sunlight into chemical energy in plants.",
        summary="Photosynthesis converts light to energy",
    )
    await tmp_db.save_page(page)
    await embed_and_store_page(tmp_db, page)

    results = await search_pages(
        tmp_db, "how do plants use sunlight?", match_threshold=0.3,
    )
    returned_ids = [p.id for p, _ in results]
    assert page.id in returned_ids
    similarity = next(s for p, s in results if p.id == page.id)
    assert 0.3 < similarity <= 1.0
