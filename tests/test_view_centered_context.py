"""Tests for build_view_centered_context.

The view-centered context builder replaces the parent-question scan in the
default context flow with a rendered Current View (when one exists), plus a
key-references tail and embedding neighbors filling remaining budget. When no
View material is available it falls back to the embedding context path.
"""

import logging

import pytest

from rumil.context import (
    EmbeddingBasedContextResult,
    build_view_centered_context,
)
from rumil.models import (
    ConsiderationDirection,
    LinkRole,
    LinkType,
    Page,
    PageLayer,
    PageLink,
    PageType,
    Workspace,
)


def _question(headline: str = "Will LLMs accelerate AI R&D?", **kw) -> Page:
    return Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=headline,
        headline=headline,
        abstract=f"Abstract of {headline}",
        **kw,
    )


def _claim(
    headline: str,
    *,
    credence: int = 6,
    robustness: int = 3,
    importance: int = 1,
    content: str | None = None,
) -> Page:
    return Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=content or f"Full content for: {headline}",
        headline=headline,
        abstract=f"Abstract of {headline}",
        credence=credence,
        robustness=robustness,
        importance=importance,
    )


def _consideration_link(claim_id: str, question_id: str) -> PageLink:
    return PageLink(
        from_page_id=claim_id,
        to_page_id=question_id,
        link_type=LinkType.CONSIDERATION,
        direction=ConsiderationDirection.SUPPORTS,
        strength=3.0,
        role=LinkRole.DIRECT,
    )


FAKE_EMBEDDING = [0.1] * 1024


@pytest.fixture
def patch_embeddings(mocker):
    """Keep the embedding fallback path inert — return no neighbors by default."""
    mocker.patch(
        "rumil.context.embed_query",
        new_callable=mocker.AsyncMock,
        return_value=FAKE_EMBEDDING,
    )
    mocker.patch(
        "rumil.context.search_pages_by_vector",
        new_callable=mocker.AsyncMock,
        return_value=[],
    )


def _make_mock_db(mocker, *, question: Page, considerations: list[tuple[Page, PageLink]]):
    """Assemble a mock DB that answers the calls build_view makes."""
    db = mocker.AsyncMock()
    db.get_page = mocker.AsyncMock(return_value=question)
    db.get_considerations_for_question = mocker.AsyncMock(return_value=considerations)
    db.get_child_questions_with_links = mocker.AsyncMock(return_value=[])
    db.get_judgements_for_question = mocker.AsyncMock(return_value=[])
    db.get_judgements_for_questions = mocker.AsyncMock(return_value={})
    db.get_links_from_many = mocker.AsyncMock(return_value={})
    db.get_links_to = mocker.AsyncMock(return_value=[])
    db.get_pages_by_ids = mocker.AsyncMock(return_value={c.id: c for c, _ in considerations})
    return db


async def test_view_centered_renders_view_sections_when_view_exists(patch_embeddings, mocker):
    """When a View has items, the rendered context contains View section headers."""
    q = _question()
    core_claim = _claim("Core finding about scaling", credence=7, robustness=4, importance=1)
    db = _make_mock_db(
        mocker,
        question=q,
        considerations=[(core_claim, _consideration_link(core_claim.id, q.id))],
    )

    result = await build_view_centered_context(q.headline, db, scope_question_id=q.id)

    assert isinstance(result, EmbeddingBasedContextResult)
    assert "## Current View" in result.context_text
    assert "## Core Findings" in result.context_text
    assert core_claim.headline in result.context_text
    assert core_claim.id in result.page_ids


async def test_view_centered_falls_back_when_no_view(patch_embeddings, mocker, caplog):
    """With no considerations/judgements/children, fall back to embedding context."""
    q = _question()
    db = _make_mock_db(mocker, question=q, considerations=[])

    with caplog.at_level(logging.DEBUG, logger="rumil.context"):
        result = await build_view_centered_context(q.headline, db, scope_question_id=q.id)

    assert isinstance(result, EmbeddingBasedContextResult)
    assert "## Current View" not in result.context_text
    assert any("falling back to embedding context" in rec.message for rec in caplog.records)


async def test_view_centered_honors_char_budget(patch_embeddings, mocker):
    """With a tiny per-tier budget, the total rendered context stays bounded."""
    q = _question()
    considerations: list[tuple[Page, PageLink]] = []
    for i in range(10):
        c = _claim(
            f"Claim number {i} with long content " + "X" * 500,
            importance=0,
            credence=7,
        )
        considerations.append((c, _consideration_link(c.id, q.id)))
    db = _make_mock_db(mocker, question=q, considerations=considerations)

    tiny = 400
    result = await build_view_centered_context(
        q.headline,
        db,
        scope_question_id=q.id,
        full_page_char_budget=tiny,
        abstract_page_char_budget=tiny,
        summary_page_char_budget=tiny,
        distillation_page_char_budget=tiny,
        top_k_references=0,
    )

    total_budget_plus_slack = 4 * tiny + 2_000
    assert len(result.context_text) <= total_budget_plus_slack


async def test_view_centered_returns_drop_in_compatible_shape(patch_embeddings, mocker):
    """Return value must have all fields the embedding result exposes."""
    q = _question()
    c = _claim("A claim", importance=1, credence=6)
    db = _make_mock_db(mocker, question=q, considerations=[(c, _consideration_link(c.id, q.id))])

    result = await build_view_centered_context(q.headline, db, scope_question_id=q.id)

    for field in (
        "context_text",
        "page_ids",
        "full_page_ids",
        "abstract_page_ids",
        "summary_page_ids",
        "distillation_page_ids",
        "budget_usage",
    ):
        assert hasattr(result, field), f"missing field {field} on result"
    assert isinstance(result.context_text, str)
    assert isinstance(result.page_ids, list)


async def test_view_centered_includes_key_references_for_top_items(patch_embeddings, mocker):
    """Top-K items appear under a '## Key References' section with full content."""
    q = _question()
    top = _claim(
        "Top important finding",
        importance=0,
        credence=8,
        robustness=4,
        content="Distinctive-reference-marker-A1B2C3 — a uniquely identifying body",
    )
    mid = _claim("Less important finding", importance=2, credence=6, robustness=3)
    considerations = [
        (top, _consideration_link(top.id, q.id)),
        (mid, _consideration_link(mid.id, q.id)),
    ]
    db = _make_mock_db(mocker, question=q, considerations=considerations)

    result = await build_view_centered_context(
        q.headline,
        db,
        scope_question_id=q.id,
        top_k_references=1,
    )

    assert "## Key References" in result.context_text
    assert "Distinctive-reference-marker-A1B2C3" in result.context_text


async def test_embedding_context_used_when_setting_off(mocker):
    """The EmbeddingContext builder uses the legacy embedding path when the flag is off.

    This exercises the opt-in wiring at the call-builder level without needing
    a CallInfra: we patch both context functions and assert which one is invoked.
    """
    from rumil.calls import context_builders
    from rumil.settings import override_settings

    called = {"embedding": 0, "view_centered": 0}

    async def fake_embedding(*args, **kwargs):
        called["embedding"] += 1
        return EmbeddingBasedContextResult(
            context_text="EMB",
            page_ids=[],
            full_page_ids=[],
            abstract_page_ids=[],
            summary_page_ids=[],
        )

    async def fake_view(*args, **kwargs):
        called["view_centered"] += 1
        return EmbeddingBasedContextResult(
            context_text="VIEW",
            page_ids=[],
            full_page_ids=[],
            abstract_page_ids=[],
            summary_page_ids=[],
        )

    mocker.patch.object(
        context_builders, "build_embedding_based_context", side_effect=fake_embedding
    )
    mocker.patch.object(context_builders, "build_view_centered_context", side_effect=fake_view)

    from rumil.models import CallType

    builder = context_builders.EmbeddingContext(CallType.FIND_CONSIDERATIONS)

    infra = mocker.MagicMock()
    infra.question_id = "qid"
    infra.db = mocker.AsyncMock()
    infra.db.get_page = mocker.AsyncMock(return_value=_question())
    infra.call.context_page_ids = []
    infra.trace.record = mocker.AsyncMock()
    mocker.patch.object(
        context_builders, "resolve_page_refs", new_callable=mocker.AsyncMock, return_value=[]
    )

    with override_settings(context_view_centered=False):
        await builder.build_context(infra)
    assert called["embedding"] == 1
    assert called["view_centered"] == 0

    with override_settings(context_view_centered=True):
        await builder.build_context(infra)
    assert called["embedding"] == 1
    assert called["view_centered"] == 1
