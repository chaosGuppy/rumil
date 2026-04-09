"""Tests for in_light_of resolution and its use in reassess_question."""

import pytest

from rumil.clean.common import reassess_question, resolve_in_light_of
from rumil.models import (
    Call,
    CallStatus,
    CallType,
    LinkType,
    Page,
    PageLayer,
    PageLink,
    PageType,
    Workspace,
)
from rumil.tracing.tracer import CallTrace


def _make_page(page_type: PageType, headline: str, **kwargs) -> Page:
    return Page(
        page_type=page_type,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=f"Content for {headline}",
        headline=headline,
        **kwargs,
    )


async def test_resolve_in_light_of_returns_claim_pages(tmp_db):
    claim = _make_page(PageType.CLAIM, "Some claim")
    await tmp_db.save_page(claim)

    resolved = await resolve_in_light_of([claim.id[:8]], tmp_db)

    assert len(resolved) == 1
    assert resolved[0].id == claim.id


async def test_resolve_in_light_of_swaps_question_for_judgement(tmp_db):
    question = _make_page(PageType.QUESTION, "Some question")
    await tmp_db.save_page(question)

    judgement = _make_page(PageType.JUDGEMENT, "Judgement on question")
    await tmp_db.save_page(judgement)
    await tmp_db.save_link(PageLink(
        from_page_id=judgement.id,
        to_page_id=question.id,
        link_type=LinkType.RELATED,
    ))

    resolved = await resolve_in_light_of([question.id[:8]], tmp_db)

    assert len(resolved) == 1
    assert resolved[0].id == judgement.id


async def test_resolve_in_light_of_returns_question_when_no_judgement(tmp_db):
    question = _make_page(PageType.QUESTION, "Unjudged question")
    await tmp_db.save_page(question)

    resolved = await resolve_in_light_of([question.id[:8]], tmp_db)

    assert len(resolved) == 1
    assert resolved[0].id == question.id


async def test_resolve_in_light_of_skips_unresolvable_ids(tmp_db):
    resolved = await resolve_in_light_of(["deadbeef"], tmp_db)

    assert resolved == []


async def test_resolve_in_light_of_skips_superseded_pages(tmp_db):
    old_claim = _make_page(PageType.CLAIM, "Old claim")
    new_claim = _make_page(PageType.CLAIM, "New claim")
    await tmp_db.save_page(old_claim)
    await tmp_db.save_page(new_claim)
    await tmp_db.supersede_page(old_claim.id, new_claim.id)

    resolved = await resolve_in_light_of([old_claim.id[:8]], tmp_db)

    assert resolved == []


async def test_reassess_question_passes_context_page_ids(
    tmp_db, question_page, mocker,
):
    """The child assess call should have context_page_ids set to the resolved
    in_light_of pages."""
    claim = _make_page(PageType.CLAIM, "Updated claim")
    await tmp_db.save_page(claim)

    parent_call = Call(
        call_type=CallType.FEEDBACK_UPDATE,
        workspace=Workspace.RESEARCH,
        scope_page_id=question_page.id,
        status=CallStatus.RUNNING,
    )
    await tmp_db.save_call(parent_call)
    trace = CallTrace(parent_call.id, tmp_db)

    # Mock CallRunner.run to avoid LLM calls — we only care about call creation
    mocker.patch("rumil.clean.common.ASSESS_CALL_CLASSES", {
        "default": type("FakeAssess", (), {
            "__init__": lambda self, *a, **kw: None,
            "run": mocker.AsyncMock(),
        }),
    })

    await reassess_question(
        question_page.id[:8], [claim.id[:8]], parent_call, tmp_db, trace,
    )

    # Find the child assess call
    children = await tmp_db.get_child_calls(parent_call.id)
    assess_calls = [c for c in children if c.call_type == CallType.ASSESS]
    assert len(assess_calls) == 1
    assert claim.id in assess_calls[0].context_page_ids


async def test_reassess_question_resolves_question_to_judgement_in_context(
    tmp_db, question_page, mocker,
):
    """When in_light_of contains a question with a judgement, the child assess
    call's context_page_ids should contain the judgement ID, not the question."""
    sub_question = _make_page(PageType.QUESTION, "Sub-question")
    await tmp_db.save_page(sub_question)
    judgement = _make_page(PageType.JUDGEMENT, "Sub-question judgement")
    await tmp_db.save_page(judgement)
    await tmp_db.save_link(PageLink(
        from_page_id=judgement.id,
        to_page_id=sub_question.id,
        link_type=LinkType.RELATED,
    ))

    parent_call = Call(
        call_type=CallType.FEEDBACK_UPDATE,
        workspace=Workspace.RESEARCH,
        scope_page_id=question_page.id,
        status=CallStatus.RUNNING,
    )
    await tmp_db.save_call(parent_call)
    trace = CallTrace(parent_call.id, tmp_db)

    mocker.patch("rumil.clean.common.ASSESS_CALL_CLASSES", {
        "default": type("FakeAssess", (), {
            "__init__": lambda self, *a, **kw: None,
            "run": mocker.AsyncMock(),
        }),
    })

    await reassess_question(
        question_page.id[:8], [sub_question.id[:8]], parent_call, tmp_db, trace,
    )

    children = await tmp_db.get_child_calls(parent_call.id)
    assess_calls = [c for c in children if c.call_type == CallType.ASSESS]
    assert len(assess_calls) == 1
    assert judgement.id in assess_calls[0].context_page_ids
    assert sub_question.id not in assess_calls[0].context_page_ids
