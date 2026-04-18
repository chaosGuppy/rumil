"""Tests for question_triage (assess-on-creation)."""

import pytest

from rumil.llm import StructuredCallResult
from rumil.models import Page, PageLayer, PageType, Workspace
from rumil.question_triage import (
    TriageVerdict,
    auto_triage_and_save,
    triage_question,
)
from rumil.settings import override_settings


def _question(headline: str, abstract: str = "") -> Page:
    return Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=abstract or headline,
        headline=headline,
        abstract=abstract,
        provenance_model="human",
    )


def _verdict(**overrides) -> TriageVerdict:
    base = {
        "fertility_score": 5,
        "is_duplicate": False,
        "duplicate_of": None,
        "is_ill_posed": False,
        "ill_posed_reason": "",
        "scope_appropriate": True,
        "scope_reason": "",
        "reasoning": "Novel, well-posed question worth investigating.",
    }
    base.update(overrides)
    return TriageVerdict(**base)


def _mock_structured_call(mocker, verdict: TriageVerdict):
    """Patch structured_call to return the given verdict. Returns the mock."""
    return mocker.patch(
        "rumil.question_triage.structured_call",
        return_value=StructuredCallResult(parsed=verdict, response_text=""),
    )


def _mock_embedding_search(mocker, neighbors: list[tuple[Page, float]] | None = None):
    """Patch embed_query + search_pages_by_vector to avoid API + DB calls."""
    mocker.patch(
        "rumil.question_triage.embed_query",
        return_value=[0.0] * 1024,
    )
    mocker.patch(
        "rumil.question_triage.search_pages_by_vector",
        return_value=neighbors or [],
    )


async def test_triage_novel_question_returns_high_fertility(tmp_db, mocker):
    _mock_embedding_search(mocker, neighbors=[])
    call_mock = _mock_structured_call(mocker, _verdict(fertility_score=5))

    verdict = await triage_question(
        tmp_db,
        question_headline="What is the base rate for AI firms failing in year 2?",
        question_abstract="",
        parent_question=None,
    )

    assert verdict.fertility_score == 5
    assert verdict.is_duplicate is False
    assert verdict.duplicate_of is None
    assert call_mock.call_count == 1


async def test_triage_duplicate_question(tmp_db, mocker):
    existing = _question(
        "How often do AI companies go bankrupt in their second year?",
        abstract="Looking at historical data on AI startup failures.",
    )
    await tmp_db.save_page(existing)

    _mock_embedding_search(mocker, neighbors=[(existing, 0.92)])
    _mock_structured_call(
        mocker,
        _verdict(
            fertility_score=1,
            is_duplicate=True,
            duplicate_of=existing.id,
            reasoning="This is the same question as the existing one, just reworded.",
        ),
    )

    verdict = await triage_question(
        tmp_db,
        question_headline="What is the base rate for AI firms failing in year 2?",
        question_abstract="",
        parent_question=None,
    )

    assert verdict.is_duplicate is True
    assert verdict.duplicate_of == existing.id
    assert verdict.fertility_score == 1


async def test_auto_triage_and_save_writes_extra(tmp_db, mocker):
    page = _question("A fresh new research question")
    await tmp_db.save_page(page)

    _mock_embedding_search(mocker, neighbors=[])
    _mock_structured_call(
        mocker,
        _verdict(fertility_score=4, reasoning="Solid question."),
    )

    with override_settings(rumil_test_mode="1", enable_question_triage=True):
        payload = await auto_triage_and_save(tmp_db, page.id, parent_id=None)

    assert payload is not None
    assert payload["fertility_score"] == 4

    stored = await tmp_db.get_page(page.id)
    assert stored is not None
    assert "triage" in stored.extra
    assert stored.extra["triage"]["fertility_score"] == 4
    assert stored.extra["triage"]["is_duplicate"] is False
    assert "triaged_at" in stored.extra["triage"]


async def test_auto_triage_swallows_llm_failures(tmp_db, mocker):
    page = _question("A question whose triage will blow up")
    await tmp_db.save_page(page)

    _mock_embedding_search(mocker, neighbors=[])
    mocker.patch(
        "rumil.question_triage.structured_call",
        side_effect=RuntimeError("LLM exploded"),
    )

    with override_settings(rumil_test_mode="1", enable_question_triage=True):
        payload = await auto_triage_and_save(tmp_db, page.id, parent_id=None)

    assert payload is None
    stored = await tmp_db.get_page(page.id)
    assert stored is not None
    assert "triage" not in (stored.extra or {})


async def test_auto_triage_disabled_is_noop(tmp_db, mocker):
    page = _question("Triage-disabled question")
    await tmp_db.save_page(page)

    call_mock = mocker.patch(
        "rumil.question_triage.structured_call",
        side_effect=AssertionError("structured_call should not run when disabled"),
    )
    embed_mock = mocker.patch(
        "rumil.question_triage.embed_query",
        side_effect=AssertionError("embed_query should not run when disabled"),
    )

    with override_settings(rumil_test_mode="1", enable_question_triage=False):
        payload = await auto_triage_and_save(tmp_db, page.id, parent_id=None)

    assert payload is None
    assert call_mock.call_count == 0
    assert embed_mock.call_count == 0
    stored = await tmp_db.get_page(page.id)
    assert stored is not None
    assert "triage" not in (stored.extra or {})


async def test_triage_includes_parent_headline_in_prompt(tmp_db, mocker):
    parent = _question(
        "How should we price GPT-5 tokens?",
        abstract="Pricing decisions for a frontier model.",
    )
    await tmp_db.save_page(parent)

    _mock_embedding_search(mocker, neighbors=[])
    call_mock = _mock_structured_call(mocker, _verdict())

    await triage_question(
        tmp_db,
        question_headline="What does the competitive landscape look like for inference?",
        question_abstract="Who are the main competitors and what do they charge?",
        parent_question=parent,
    )

    assert call_mock.call_count == 1
    kwargs = call_mock.call_args.kwargs
    user_message = kwargs.get("user_message") or call_mock.call_args.args[1]
    assert parent.headline in user_message
    assert "Pricing decisions for a frontier model." in user_message
