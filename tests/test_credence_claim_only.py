"""Tests that credence is restricted to claim pages.

These exercise the core invariant introduced by the "only claims have
credence" refactor:

- the CREATE_JUDGEMENT payload no longer accepts a credence field
- create_page() never persists credence on non-claim pages
- update_epistemic rejects credence updates on non-claims but allows
  robustness-only updates
"""

import pytest_asyncio

from rumil.models import (
    Call,
    CallStatus,
    CallType,
    Page,
    PageLayer,
    PageType,
    Workspace,
)
from rumil.moves.base import CreatePagePayload, ScoredPagePayload, create_page
from rumil.moves.create_claim import CreateClaimPayload
from rumil.moves.create_claim import execute as execute_create_claim
from rumil.moves.create_judgement import CreateJudgementPayload
from rumil.moves.update_epistemic import UpdateEpistemicPayload
from rumil.moves.update_epistemic import execute as execute_update_epistemic


@pytest_asyncio.fixture
async def question_page(tmp_db):
    page = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="Does cheese have flavour?",
        headline="Does cheese have flavour?",
    )
    await tmp_db.save_page(page)
    return page


@pytest_asyncio.fixture
async def assess_call(tmp_db, question_page):
    call = Call(
        call_type=CallType.ASSESS,
        workspace=Workspace.RESEARCH,
        scope_page_id=question_page.id,
        status=CallStatus.PENDING,
    )
    await tmp_db.save_call(call)
    return call


@pytest_asyncio.fixture
async def find_cons_call(tmp_db, question_page):
    call = Call(
        call_type=CallType.FIND_CONSIDERATIONS,
        workspace=Workspace.RESEARCH,
        scope_page_id=question_page.id,
        status=CallStatus.PENDING,
    )
    await tmp_db.save_call(call)
    return call


def test_credence_absent_from_judgement_tool_schema():
    """The JSON schema the LLM sees for create_judgement must not offer credence."""
    schema = CreateJudgementPayload.model_json_schema()
    assert "credence" not in schema.get("properties", {})


def test_credence_absent_from_base_payload_schema():
    """Credence belongs to CreateClaimPayload only, not to the shared base."""
    schema = CreatePagePayload.model_json_schema()
    assert "credence" not in schema.get("properties", {})


def test_credence_present_in_claim_tool_schema():
    """Sanity check: the create_claim schema still advertises credence."""
    schema = CreateClaimPayload.model_json_schema()
    assert "credence" in schema.get("properties", {})


async def test_create_page_on_summary_has_null_credence(tmp_db, assess_call):
    """Non-claim page types get credence=None regardless of caller intent."""
    payload = ScoredPagePayload(
        headline="Summary of cheese findings",
        content="Cheese has been studied at length.",
        robustness=2,
        robustness_reasoning="broad literature but some gaps remain",
        workspace="research",
        supersedes=None,
        change_magnitude=None,
    )
    result = await create_page(
        payload,
        assess_call,
        tmp_db,
        PageType.SUMMARY,
        PageLayer.SQUIDGY,
        robustness=payload.robustness,
        robustness_reasoning=payload.robustness_reasoning,
    )
    assert result.created_page_id
    page = await tmp_db.get_page(result.created_page_id)
    assert page is not None
    assert page.credence is None
    assert page.robustness == 2


async def test_create_page_on_judgement_has_null_credence(tmp_db, assess_call):
    """Judgement pages never carry credence."""
    payload = CreateJudgementPayload(
        headline="Cheese is tasty on balance",
        content="Weighing the considerations, cheese is tasty.",
        robustness=3,
        robustness_reasoning="reasonable weighing of considerations so far",
        workspace="research",
        supersedes=None,
        change_magnitude=None,
        key_dependencies=None,
        sensitivity_analysis=None,
        fruit_remaining=None,
    )
    result = await create_page(
        payload,
        assess_call,
        tmp_db,
        PageType.JUDGEMENT,
        PageLayer.SQUIDGY,
        robustness=payload.robustness,
        robustness_reasoning=payload.robustness_reasoning,
    )
    assert result.created_page_id
    page = await tmp_db.get_page(result.created_page_id)
    assert page is not None
    assert page.page_type == PageType.JUDGEMENT
    assert page.credence is None
    assert page.robustness == 3


async def test_create_claim_persists_credence(tmp_db, find_cons_call):
    """Sanity check: claims still carry the credence their move sets."""
    payload = CreateClaimPayload(
        headline="Cheddar pairs well with apples",
        content="Cheddar has sufficient sharpness to complement apple sweetness.",
        credence=7,
        credence_reasoning="widely-held culinary pairing with few credible dissenters",
        robustness=2,
        robustness_reasoning="subjective taste, could shift with more systematic tasting data",
        workspace="research",
        supersedes=None,
        change_magnitude=None,
    )
    result = await execute_create_claim(payload, find_cons_call, tmp_db)
    assert result.created_page_id
    page = await tmp_db.get_page(result.created_page_id)
    assert page is not None
    assert page.credence == 7
    assert page.robustness == 2


async def test_update_epistemic_rejects_credence_on_judgement(tmp_db, assess_call, question_page):
    """Credence updates are claim-only; a judgement must be rejected."""
    judgement = Page(
        page_type=PageType.JUDGEMENT,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="Cheese is tasty.",
        headline="Cheese is tasty.",
        robustness=3,
    )
    await tmp_db.save_page(judgement)

    payload = UpdateEpistemicPayload(
        page_id=judgement.id,
        credence=7,
        credence_reasoning="try to bump credence on a judgement",
    )
    result = await execute_update_epistemic(payload, assess_call, tmp_db)
    assert "claim" in result.message.lower()
    refreshed = await tmp_db.get_page(judgement.id)
    assert refreshed is not None
    assert refreshed.credence is None


async def test_update_epistemic_allows_robustness_on_judgement(tmp_db, assess_call, question_page):
    """Robustness-only updates on non-claim pages go through."""
    judgement = Page(
        page_type=PageType.JUDGEMENT,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="Cheese is tasty.",
        headline="Cheese is tasty.",
        robustness=3,
    )
    await tmp_db.save_page(judgement)

    payload = UpdateEpistemicPayload(
        page_id=judgement.id,
        robustness=4,
        robustness_reasoning="further investigation firmed this up",
    )
    result = await execute_update_epistemic(payload, assess_call, tmp_db)
    assert "updated" in result.message.lower()

    refreshed = await tmp_db.get_page(judgement.id)
    assert refreshed is not None
    assert refreshed.robustness == 4
    assert refreshed.credence is None


async def test_update_epistemic_rejects_empty_payload(tmp_db, assess_call):
    claim = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="A claim.",
        headline="A claim.",
        credence=5,
        robustness=2,
    )
    await tmp_db.save_page(claim)

    payload = UpdateEpistemicPayload(page_id=claim.id)
    result = await execute_update_epistemic(payload, assess_call, tmp_db)
    assert "at least one" in result.message.lower()
