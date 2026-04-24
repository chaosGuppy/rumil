"""Tests for generate_artefact and critique_artefact.

- Non-LLM: build_system_prompt(include_preamble=False) skips preamble;
  SpecOnlyContext renders the task + spec items only; CritiqueContext raises
  when no artefact exists yet.
- Integration: end-to-end generate_artefact produces a hidden ARTEFACT page
  with ARTEFACT_OF + GENERATED_FROM links; critique_artefact produces a
  hidden JUDGEMENT linked via CRITIQUE_OF.
"""

import pytest
import pytest_asyncio

from rumil.calls.context_builders import (
    CritiqueContext,
    SpecOnlyContext,
    _latest_artefact_for_task,
)
from rumil.calls.critique_artefact import CritiqueArtefactCall
from rumil.calls.generate_artefact import GenerateArtefactCall
from rumil.calls.stages import CallInfra
from rumil.llm import build_system_prompt
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
from rumil.moves.base import MoveState
from rumil.tracing.tracer import CallTrace


def test_build_system_prompt_omits_preamble_when_flagged():
    """include_preamble=False should return just the call-type instructions."""
    with_preamble = build_system_prompt("generate_artefact", include_preamble=True)
    without_preamble = build_system_prompt("generate_artefact", include_preamble=False)

    assert len(without_preamble) < len(with_preamble)
    assert "Rumil" not in without_preamble
    assert "Artefact Writer" in without_preamble


@pytest_asyncio.fixture
async def artefact_task(tmp_db):
    page = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=(
            "Write a two-paragraph blurb describing what happens during "
            "photosynthesis, suitable for a high-school biology poster."
        ),
        headline="Photosynthesis blurb for a biology poster",
        hidden=True,
    )
    await tmp_db.save_page(page)
    return page


async def _seed_spec_items(db, task_id, items):
    created = []
    for headline, content in items:
        spec = Page(
            page_type=PageType.SPEC_ITEM,
            layer=PageLayer.SQUIDGY,
            workspace=Workspace.RESEARCH,
            content=content,
            headline=headline,
            hidden=True,
        )
        await db.save_page(spec)
        await db.save_link(
            PageLink(
                from_page_id=spec.id,
                to_page_id=task_id,
                link_type=LinkType.SPEC_OF,
            )
        )
        created.append(spec)
    return created


async def test_spec_only_context_renders_task_and_spec(tmp_db, artefact_task):
    """SpecOnlyContext output contains the task and every active spec item."""
    await _seed_spec_items(
        tmp_db,
        artefact_task.id,
        [
            ("Plain-English diction", "Avoid jargon; define any technical term inline."),
            ("Two paragraphs", "Exactly two paragraphs, no headings."),
        ],
    )

    call = Call(
        call_type=CallType.GENERATE_ARTEFACT,
        workspace=Workspace.RESEARCH,
        scope_page_id=artefact_task.id,
        status=CallStatus.PENDING,
    )
    await tmp_db.save_call(call)

    infra = CallInfra(
        question_id=artefact_task.id,
        call=call,
        db=tmp_db,
        trace=CallTrace(call.id, tmp_db),
        state=MoveState(call, tmp_db),
    )
    builder = SpecOnlyContext()
    ctx = await builder.build_context(infra)

    assert "photosynthesis" in ctx.context_text.lower()
    assert "Plain-English diction" in ctx.context_text
    assert "Two paragraphs" in ctx.context_text
    assert artefact_task.id in ctx.working_page_ids
    assert len(ctx.working_page_ids) == 3


async def test_critique_context_errors_when_no_artefact(tmp_db, artefact_task):
    """CritiqueContext requires an artefact; surface a clear error otherwise."""
    call = Call(
        call_type=CallType.CRITIQUE_ARTEFACT,
        workspace=Workspace.RESEARCH,
        scope_page_id=artefact_task.id,
        status=CallStatus.PENDING,
    )
    await tmp_db.save_call(call)

    infra = CallInfra(
        question_id=artefact_task.id,
        call=call,
        db=tmp_db,
        trace=CallTrace(call.id, tmp_db),
        state=MoveState(call, tmp_db),
    )
    with pytest.raises(ValueError, match="no artefact"):
        await CritiqueContext().build_context(infra)


async def test_latest_artefact_for_task_returns_most_recent(tmp_db, artefact_task):
    """Helper selects the most recent active ARTEFACT linked to the task."""
    assert await _latest_artefact_for_task(artefact_task.id, tmp_db) is None

    older = Page(
        page_type=PageType.ARTEFACT,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="first draft",
        headline="Draft 1",
        hidden=True,
    )
    await tmp_db.save_page(older)
    await tmp_db.save_link(
        PageLink(
            from_page_id=older.id,
            to_page_id=artefact_task.id,
            link_type=LinkType.ARTEFACT_OF,
        )
    )

    newer = Page(
        page_type=PageType.ARTEFACT,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="second draft",
        headline="Draft 2",
        hidden=True,
    )
    await tmp_db.save_page(newer)
    await tmp_db.save_link(
        PageLink(
            from_page_id=newer.id,
            to_page_id=artefact_task.id,
            link_type=LinkType.ARTEFACT_OF,
        )
    )

    latest = await _latest_artefact_for_task(artefact_task.id, tmp_db)
    assert latest is not None
    assert latest.id == newer.id


@pytest.mark.integration
async def test_generate_artefact_end_to_end(tmp_db, artefact_task):
    """generate_artefact creates an ARTEFACT linked ARTEFACT_OF + GENERATED_FROM."""
    specs = await _seed_spec_items(
        tmp_db,
        artefact_task.id,
        [
            ("Plain English only", "Avoid jargon; define any technical term inline."),
            ("Two paragraphs", "Output must be exactly two paragraphs, no headings."),
            (
                "Name inputs and outputs",
                "Mention what goes in (light, CO2, water) and what comes out (sugar, oxygen).",
            ),
        ],
    )

    call = Call(
        call_type=CallType.GENERATE_ARTEFACT,
        workspace=Workspace.RESEARCH,
        scope_page_id=artefact_task.id,
        status=CallStatus.PENDING,
    )
    await tmp_db.save_call(call)

    runner = GenerateArtefactCall(artefact_task.id, call, tmp_db)
    await runner.run()

    refreshed = await tmp_db.get_call(call.id)
    assert refreshed.status == CallStatus.COMPLETE

    artefact = await _latest_artefact_for_task(artefact_task.id, tmp_db)
    assert artefact is not None
    assert artefact.page_type == PageType.ARTEFACT
    assert artefact.hidden is True
    assert len(artefact.content) > 50

    outgoing = await tmp_db.get_links_from(artefact.id)
    artefact_of = [l for l in outgoing if l.link_type == LinkType.ARTEFACT_OF]
    generated_from = [l for l in outgoing if l.link_type == LinkType.GENERATED_FROM]
    assert len(artefact_of) == 1
    assert artefact_of[0].to_page_id == artefact_task.id
    assert {l.to_page_id for l in generated_from} == {s.id for s in specs}


@pytest.mark.integration
async def test_critique_artefact_end_to_end(tmp_db, artefact_task):
    """critique_artefact creates a JUDGEMENT linked CRITIQUE_OF to the artefact."""
    artefact = Page(
        page_type=PageType.ARTEFACT,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=(
            "Photosynthesis is how plants eat light. Leaves take in sunshine, "
            "drink water from the roots, and breathe in air. Inside the leaf, "
            "tiny green parts use the light to mix the air and water into food "
            "for the plant. The plant then lets out fresh oxygen, which is "
            "what we breathe. Without this, there would be no food and no air "
            "to breathe."
        ),
        headline="A simple blurb on photosynthesis",
        hidden=True,
    )
    await tmp_db.save_page(artefact)
    await tmp_db.save_link(
        PageLink(
            from_page_id=artefact.id,
            to_page_id=artefact_task.id,
            link_type=LinkType.ARTEFACT_OF,
        )
    )

    call = Call(
        call_type=CallType.CRITIQUE_ARTEFACT,
        workspace=Workspace.RESEARCH,
        scope_page_id=artefact_task.id,
        status=CallStatus.PENDING,
    )
    await tmp_db.save_call(call)

    runner = CritiqueArtefactCall(artefact_task.id, call, tmp_db)
    await runner.run()

    refreshed = await tmp_db.get_call(call.id)
    assert refreshed.status == CallStatus.COMPLETE

    links_to_artefact = await tmp_db.get_links_to(artefact.id)
    critique_links = [l for l in links_to_artefact if l.link_type == LinkType.CRITIQUE_OF]
    assert len(critique_links) >= 1

    critique_pages = await tmp_db.get_pages_by_ids([l.from_page_id for l in critique_links])
    assert all(p.page_type == PageType.JUDGEMENT for p in critique_pages.values())
    assert all(p.hidden for p in critique_pages.values())
    for p in critique_pages.values():
        assert "grade" in p.extra
        assert isinstance(p.extra["grade"], int)
