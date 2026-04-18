"""Tests for create_question semantic dedup against existing questions.

The bug: three trivially-reworded variants of the same subquestion were being
created as three separate pages, e.g. "How much high-quality training data
remains available for LLM pretraining?" appearing as three near-identical
pages. Fix: before creating a new question page, embed the candidate
headline and cosine-check against existing project questions. Above the
configured threshold, reuse the existing page's ID (and still attach any
requested parent links to it).

These tests mock the embedding boundary so they stay fast and deterministic.
"""

from collections.abc import Sequence

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
from rumil.moves.create_question import CreateQuestionPayload
from rumil.moves.create_question import execute as execute_create_question
from rumil.moves.link_child_question import ChildQuestionLinkFields
from rumil.settings import override_settings

FAKE_EMBEDDING = [0.1] * 1024


@pytest_asyncio.fixture
async def parent_question(tmp_db):
    page = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="What will pretraining look like in 2027?",
        headline="What will pretraining look like in 2027?",
    )
    await tmp_db.save_page(page)
    return page


@pytest_asyncio.fixture
async def call(tmp_db, parent_question):
    c = Call(
        call_type=CallType.FIND_CONSIDERATIONS,
        workspace=Workspace.RESEARCH,
        scope_page_id=parent_question.id,
        status=CallStatus.PENDING,
    )
    await tmp_db.save_call(c)
    return c


def _patch_embeddings(
    mocker,
    search_results: Sequence[tuple[Page, float]] | Sequence[Sequence[tuple[Page, float]]],
):
    """Patch embed_query and search_pages_by_vector inside create_question.

    If ``search_results`` is a flat sequence of (page, score), every call
    returns it. If it is a sequence of such sequences, successive calls
    return successive elements (for tests that invoke dedup twice).
    """
    mocker.patch(
        "rumil.moves.create_question.embed_query",
        new_callable=mocker.AsyncMock,
        return_value=FAKE_EMBEDDING,
    )
    is_per_call = search_results and isinstance(search_results[0], list)
    if is_per_call:
        mocker.patch(
            "rumil.moves.create_question.search_pages_by_vector",
            new_callable=mocker.AsyncMock,
            side_effect=list(search_results),
        )
    else:
        mocker.patch(
            "rumil.moves.create_question.search_pages_by_vector",
            new_callable=mocker.AsyncMock,
            return_value=list(search_results),
        )


async def _count_questions(db) -> int:
    rows = (
        await db._execute(
            db.client.table("pages")
            .select("id")
            .eq("project_id", db.project_id)
            .eq("page_type", PageType.QUESTION.value)
        )
    ).data
    return len(rows)


async def test_near_duplicate_headline_reuses_existing_page(tmp_db, call, mocker):
    """Second near-identical headline should not create a second page."""
    original = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="How much high-quality training data remains available for LLM pretraining?",
        headline="How much high-quality training data remains available for LLM pretraining?",
    )
    await tmp_db.save_page(original)

    # First call: no matches (empty search results) -> page created.
    # Second call: the first-created page is returned with high similarity.
    created_page_holder: dict = {}

    async def fake_search(*args, **kwargs):
        # Return whatever pages are in the holder plus the original, ranked.
        if created_page_holder:
            return [(created_page_holder["page"], 0.95), (original, 0.93)]
        return [(original, 0.5)]

    mocker.patch(
        "rumil.moves.create_question.embed_query",
        new_callable=mocker.AsyncMock,
        return_value=FAKE_EMBEDDING,
    )
    mocker.patch(
        "rumil.moves.create_question.search_pages_by_vector",
        side_effect=fake_search,
    )

    payload_a = CreateQuestionPayload(
        content="Supply of pretraining-grade text.",
        headline="How much high-quality text training data remains available for LLM training as of 2026?",
        credence=5,
        robustness=1,
        workspace="research",
        supersedes=None,
        change_magnitude=None,
        links=[],
    )
    result_a = await execute_create_question(payload_a, call, tmp_db)
    assert result_a.created_page_id is not None
    created_page = await tmp_db.get_page(result_a.created_page_id)
    assert created_page is not None
    created_page_holder["page"] = created_page

    count_after_a = await _count_questions(tmp_db)

    payload_b = CreateQuestionPayload(
        content="Different wording, same question.",
        headline="How much high-quality text data remains available for LLM pretraining?",
        credence=5,
        robustness=1,
        workspace="research",
        supersedes=None,
        change_magnitude=None,
        links=[],
    )
    result_b = await execute_create_question(payload_b, call, tmp_db)

    assert result_b.created_page_id is None
    assert result_b.trace_extra["deduped"] is True
    assert result_b.trace_extra["existing_page_id"] == result_a.created_page_id

    count_after_b = await _count_questions(tmp_db)
    assert count_after_b == count_after_a


async def test_truly_different_questions_both_created(tmp_db, call, mocker):
    """Two questions on different topics both get created (no false-positive dedup)."""
    _patch_embeddings(mocker, [])
    baseline = await _count_questions(tmp_db)

    payload_a = CreateQuestionPayload(
        content="Availability of pretraining text.",
        headline="What is data scarcity for LLM pretraining?",
        credence=5,
        robustness=1,
        workspace="research",
        supersedes=None,
        change_magnitude=None,
        links=[],
    )
    result_a = await execute_create_question(payload_a, call, tmp_db)
    assert result_a.created_page_id is not None

    payload_b = CreateQuestionPayload(
        content="Compute trajectory.",
        headline="What is compute scaling for LLM pretraining?",
        credence=5,
        robustness=1,
        workspace="research",
        supersedes=None,
        change_magnitude=None,
        links=[],
    )
    result_b = await execute_create_question(payload_b, call, tmp_db)
    assert result_b.created_page_id is not None
    assert result_b.created_page_id != result_a.created_page_id

    assert await _count_questions(tmp_db) - baseline == 2


async def test_dedup_redirects_parent_link_to_existing_page(tmp_db, call, parent_question, mocker):
    """When dedup fires with inline parent links, the CHILD_QUESTION link should
    point at the EXISTING page, not a newly-created one."""
    existing = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="How does data availability shape pretraining capability?",
        headline="How much high-quality training data remains available for LLM pretraining?",
    )
    await tmp_db.save_page(existing)

    _patch_embeddings(mocker, [(existing, 0.97)])

    payload = CreateQuestionPayload(
        content="Same question, slight rewording.",
        headline="How much high-quality text data remains available for LLM pretraining?",
        credence=5,
        robustness=1,
        workspace="research",
        supersedes=None,
        change_magnitude=None,
        links=[
            ChildQuestionLinkFields(
                parent_id=parent_question.id,
                reasoning="sub-question",
                role=LinkRole.STRUCTURAL,
                impact_on_parent_question=None,
            )
        ],
    )
    result = await execute_create_question(payload, call, tmp_db)

    assert result.created_page_id is None
    assert result.trace_extra["existing_page_id"] == existing.id

    parent_outgoing = await tmp_db.get_links_from(parent_question.id)
    child_question_links = [l for l in parent_outgoing if l.link_type == LinkType.CHILD_QUESTION]
    assert len(child_question_links) == 1
    assert child_question_links[0].to_page_id == existing.id


async def test_similarity_below_threshold_still_creates(tmp_db, call, mocker):
    """A match just below threshold is not treated as a duplicate."""
    existing = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="Existing question body.",
        headline="Existing question about pretraining data",
    )
    await tmp_db.save_page(existing)

    _patch_embeddings(mocker, [(existing, 0.80)])

    with override_settings(subquestion_dedup_similarity_threshold=0.85):
        payload = CreateQuestionPayload(
            content="Different enough.",
            headline="Somewhat related question about training data",
            credence=5,
            robustness=1,
            workspace="research",
            supersedes=None,
            change_magnitude=None,
            links=[],
        )
        result = await execute_create_question(payload, call, tmp_db)

    assert result.created_page_id is not None
    assert result.created_page_id != existing.id


async def test_embedding_failure_falls_back_to_creating_page(tmp_db, call, mocker):
    """If embed_query raises, the move should still create the page (and warn)."""
    mocker.patch(
        "rumil.moves.create_question.embed_query",
        new_callable=mocker.AsyncMock,
        side_effect=RuntimeError("voyage down"),
    )
    baseline = await _count_questions(tmp_db)

    payload = CreateQuestionPayload(
        content="Fallback body.",
        headline="Question for embedding-failure fallback",
        credence=5,
        robustness=1,
        workspace="research",
        supersedes=None,
        change_magnitude=None,
        links=[],
    )
    result = await execute_create_question(payload, call, tmp_db)

    assert result.created_page_id is not None
    assert await _count_questions(tmp_db) - baseline == 1


async def test_dedup_ignores_non_question_matches(tmp_db, call, mocker):
    """A high-similarity CLAIM must not be treated as a question duplicate."""
    claim = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="A claim body.",
        headline="Claim headline that shares wording",
        credence=6,
        robustness=2,
    )
    await tmp_db.save_page(claim)

    _patch_embeddings(mocker, [(claim, 0.99)])

    payload = CreateQuestionPayload(
        content="Question body.",
        headline="Claim headline that shares wording",
        credence=5,
        robustness=1,
        workspace="research",
        supersedes=None,
        change_magnitude=None,
        links=[],
    )
    result = await execute_create_question(payload, call, tmp_db)

    assert result.created_page_id is not None
    assert result.created_page_id != claim.id
