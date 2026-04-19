"""Tests for question_triage (assess-on-creation)."""

import uuid

import pytest

from rumil.database import DB
from rumil.llm import StructuredCallResult
from rumil.models import Page, PageLayer, PageType, Workspace
from rumil.question_triage import (
    TriageVerdict,
    _fetch_neighbors,
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


async def test_triage_scopes_neighbor_search_to_current_project(tmp_db, mocker):
    """Regression for wave-7 bug: cross-project false-positive duplicates.

    The bug: on an empty project B, the root question got duplicate=True
    because the embedding search returned matches from project A (e.g. the
    metr or redwood workspaces). Fix: neighbor search must be scoped to
    ``db.project_id``.

    This asserts the fix at the boundary where it matters: the underlying
    ``search_pages_by_vector`` call receives a ``project_id`` kwarg equal to
    the current db's project — if someone removes that scoping in the future,
    this test fails.
    """
    mocker.patch("rumil.question_triage.embed_query", return_value=[0.0] * 1024)
    search_mock = mocker.patch(
        "rumil.question_triage.search_pages_by_vector",
        return_value=[],
    )

    assert tmp_db.project_id is not None
    neighbors = await _fetch_neighbors(
        tmp_db,
        question_headline="Will frontier labs pool safety research by 2027?",
        question_abstract=None,
        exclude_ids=set(),
    )

    assert neighbors == []
    assert search_mock.call_count == 1
    kwargs = search_mock.call_args.kwargs
    assert kwargs.get("project_id") == tmp_db.project_id


async def test_triage_does_not_flag_duplicate_from_other_project(tmp_db, mocker):
    """End-to-end: a near-duplicate question in project A must not cause a
    new project-B question to be flagged as duplicate."""
    project_a, _ = await tmp_db.get_or_create_project(f"triage-other-{uuid.uuid4().hex[:8]}")
    other_db = await DB.create(run_id=str(uuid.uuid4()))
    other_db.project_id = project_a.id
    await other_db.init_budget(10)
    try:
        near_dupe = _question(
            "How often do AI companies go bankrupt in their second year?",
            abstract="Historical base-rate analysis.",
        )
        await other_db.save_page(near_dupe)

        async def fake_search(db, *_args, **kwargs):
            if kwargs.get("project_id") == project_a.id:
                return [(near_dupe, 0.98)]
            return []

        mocker.patch("rumil.question_triage.embed_query", return_value=[0.0] * 1024)
        mocker.patch(
            "rumil.question_triage.search_pages_by_vector",
            side_effect=fake_search,
        )
        _mock_structured_call(
            mocker,
            _verdict(fertility_score=5, is_duplicate=False, duplicate_of=None),
        )

        verdict = await triage_question(
            tmp_db,
            question_headline="What is the base rate for AI firms failing in year 2?",
            question_abstract="",
            parent_question=None,
        )

        assert verdict.is_duplicate is False
        assert verdict.duplicate_of is None
    finally:
        await other_db.delete_run_data(delete_project=True)


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
