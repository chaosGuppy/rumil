"""Tests for embedding creation and vector search."""

import uuid

import pytest

from rumil.embeddings import (
    EMBEDDING_DIMENSIONS,
    embed_and_store_page,
    embed_texts,
    search_pages,
)
from rumil.database import DB
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
        headline="Photosynthesis converts light to energy",
    )
    await tmp_db.save_page(page)
    await embed_and_store_page(tmp_db, page)

    results = await search_pages(
        tmp_db,
        "how do plants use sunlight?",
        match_threshold=0.3,
    )
    returned_ids = [p.id for p, _ in results]
    assert page.id in returned_ids
    similarity = next(s for p, s in results if p.id == page.id)
    assert 0.3 < similarity <= 1.0


async def test_store_and_search_with_field_filter(tmp_db):
    """Embeddings stored with a field_name can be filtered by that field."""
    page = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="Quantum entanglement links particles across distances.",
        headline="Quantum entanglement links distant particles",
    )
    page.abstract = "Entangled quantum particles share correlated states instantly."
    await tmp_db.save_page(page)
    await embed_and_store_page(tmp_db, page, field_name="abstract")

    results_with_filter = await search_pages(
        tmp_db,
        "quantum particles",
        match_threshold=0.3,
        field_name="abstract",
    )
    returned_ids = [p.id for p, _ in results_with_filter]
    assert page.id in returned_ids

    results_wrong_field = await search_pages(
        tmp_db,
        "quantum particles",
        match_threshold=0.3,
        field_name="content",
    )
    wrong_ids = [p.id for p, _ in results_wrong_field]
    assert page.id not in wrong_ids


async def test_search_with_staged_run_filter(tmp_db):
    """When db.staged is set, embedding search only returns pages from that staged run + baseline."""
    tmp_db.staged = True

    page = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="Tectonic plates drift slowly across the asthenosphere.",
        headline="Tectonic plates drift on asthenosphere",
    )
    await tmp_db.save_page(page)
    await embed_and_store_page(tmp_db, page)

    results_matching = await search_pages(
        tmp_db,
        "plate tectonics",
        match_threshold=0.3,
    )
    matching_ids = [p.id for p, _ in results_matching]
    assert page.id in matching_ids

    # A different staged run should not see this page
    other_db = await DB.create(
        run_id=str(uuid.uuid4()),
        client=tmp_db.client,
        project_id=tmp_db.project_id,
        staged=True,
    )
    results_other = await search_pages(
        other_db,
        "plate tectonics",
        match_threshold=0.3,
    )
    other_ids = [p.id for p, _ in results_other]
    assert page.id not in other_ids
