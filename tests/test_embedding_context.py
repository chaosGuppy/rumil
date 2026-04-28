"""Tests for build_embedding_based_context."""

import pytest

from rumil.context import (
    EmbeddingBasedContextResult,
    build_embedding_based_context,
    format_page,
)
from rumil.models import Page, PageDetail, PageLayer, PageType, Workspace
from rumil.settings import override_settings


def _make_page(headline: str, content: str, page_type: PageType = PageType.CLAIM) -> Page:
    return Page(
        page_type=page_type,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=content,
        headline=headline,
        credence=6,
        robustness=2,
    )


PAGES = [
    _make_page("Alpha finding", "A" * 200),
    _make_page("Beta finding", "B" * 200),
    _make_page("Gamma finding", "C" * 200),
    _make_page("Delta finding", "D" * 200),
    _make_page("Epsilon finding", "E" * 200),
]

RANKED = [(page, 0.9 - i * 0.1) for i, page in enumerate(PAGES)]

FAKE_EMBEDDING = [0.1] * 1024


@pytest.fixture
def mock_embeddings(mocker):
    mocker.patch(
        "rumil.context.embed_query",
        new_callable=mocker.AsyncMock,
        return_value=FAKE_EMBEDDING,
    )
    mocker.patch(
        "rumil.context.search_pages_by_vector",
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
        "test query",
        mock_db,
        full_page_char_budget=6_000,
        summary_page_char_budget=3_000,
    )

    assert isinstance(result, EmbeddingBasedContextResult)
    assert len(result.full_page_ids) > 0
    assert result.budget_usage["full"] <= 6_000
    assert result.budget_usage["summary"] <= 3_000
    assert result.distillation_page_ids == []
    assert set(result.page_ids) == set(
        result.full_page_ids + result.abstract_page_ids + result.summary_page_ids
    )


async def test_similarity_ordering(mock_embeddings, mock_db):
    """Pages appear in similarity order: highest-similarity first in full tier."""
    result = await build_embedding_based_context(
        "test query",
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
        "test query",
        mock_db,
        full_page_char_budget=100,
        abstract_page_char_budget=800,
        summary_page_char_budget=800,
    )

    assert len(result.abstract_page_ids) + len(result.summary_page_ids) > 0


async def test_format_page_headline():
    """format_page with HEADLINE detail produces the expected compact line."""
    page = _make_page("Test summary", "content")
    line = await format_page(page, PageDetail.HEADLINE)
    assert "[CLAIM C6/R2]" in line
    assert page.id[:8] in line
    assert "Test summary" in line


async def test_format_page_headline_omits_reasoning():
    """HEADLINE detail shows the score tag only — no reasoning text."""
    page = _make_page("Test summary", "content")
    page = page.model_copy(
        update={
            "credence_reasoning": "A suspiciously memorable reason",
            "robustness_reasoning": "A suspiciously memorable robustness reason",
        }
    )
    line = await format_page(page, PageDetail.HEADLINE)
    assert "suspiciously memorable" not in line


async def test_format_page_abstract_includes_reasoning():
    """ABSTRACT detail surfaces both credence_reasoning and robustness_reasoning."""
    page = _make_page("Test summary", "content")
    page = page.model_copy(
        update={
            "credence_reasoning": "Marker-credence-reasoning-xyz",
            "robustness_reasoning": "Marker-robustness-reasoning-xyz",
        }
    )
    text = await format_page(page, PageDetail.ABSTRACT)
    assert "Marker-credence-reasoning-xyz" in text
    assert "Marker-robustness-reasoning-xyz" in text


async def test_zero_budget_returns_empty(mock_embeddings, mock_db):
    """A zero budget produces no pages."""
    result = await build_embedding_based_context(
        "test query",
        mock_db,
        full_page_char_budget=0,
        abstract_page_char_budget=0,
        summary_page_char_budget=0,
        distillation_page_char_budget=0,
    )
    assert result.page_ids == []
    assert result.full_page_ids == []
    assert result.summary_page_ids == []


QUESTION_WITH_JUDGEMENT = _make_page(
    "Answered question", "Q with judgement", page_type=PageType.QUESTION
)
QUESTION_WITHOUT_JUDGEMENT = _make_page(
    "Open question", "Q without judgement", page_type=PageType.QUESTION
)
CLAIM_PAGE = _make_page("Regular claim", "A claim page")

MIXED_RANKED = [
    (QUESTION_WITH_JUDGEMENT, 0.9),
    (QUESTION_WITHOUT_JUDGEMENT, 0.8),
    (CLAIM_PAGE, 0.7),
]

JUDGEMENT_PAGE = _make_page("A judgement", "Judgement content", page_type=PageType.JUDGEMENT)


@pytest.fixture
def mock_embeddings_mixed(mocker):
    mocker.patch(
        "rumil.context.embed_query",
        new_callable=mocker.AsyncMock,
        return_value=FAKE_EMBEDDING,
    )
    mocker.patch(
        "rumil.context.search_pages_by_vector",
        new_callable=mocker.AsyncMock,
        return_value=MIXED_RANKED,
    )


async def test_require_take_filters_questions_without_take(
    mock_embeddings_mixed,
    mocker,
):
    """Questions without a take are excluded when require_take_for_questions is set."""
    db = mocker.AsyncMock()

    async def fake_get_judgements_many(qids):
        return {qid: [JUDGEMENT_PAGE] if qid == QUESTION_WITH_JUDGEMENT.id else [] for qid in qids}

    db.get_judgements_for_questions = mocker.AsyncMock(side_effect=fake_get_judgements_many)

    with override_settings(view_variant="judgement"):
        result = await build_embedding_based_context(
            "test query",
            db,
            require_take_for_questions=True,
            full_page_char_budget=10_000,
        )

    assert QUESTION_WITH_JUDGEMENT.id in result.page_ids
    assert QUESTION_WITHOUT_JUDGEMENT.id not in result.page_ids
    assert CLAIM_PAGE.id in result.page_ids


async def test_require_take_false_keeps_all_questions(
    mock_embeddings_mixed,
    mocker,
):
    """Without the flag, all questions appear regardless of take status."""
    db = mocker.AsyncMock()
    db.get_judgements_for_questions = mocker.AsyncMock(return_value={})

    with override_settings(view_variant="judgement"):
        result = await build_embedding_based_context(
            "test query",
            db,
            require_take_for_questions=False,
            full_page_char_budget=10_000,
        )

    assert QUESTION_WITH_JUDGEMENT.id in result.page_ids
    assert QUESTION_WITHOUT_JUDGEMENT.id in result.page_ids
    assert CLAIM_PAGE.id in result.page_ids
