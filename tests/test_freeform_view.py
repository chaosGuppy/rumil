"""Tests for FreeformView (CreateFreeformView / UpdateFreeformView)."""

import uuid

import pytest
import pytest_asyncio

from rumil.calls.freeform_view import (
    VIEW_KIND_FREEFORM,
    _CreateFreeformViewUpdater,
    _run_section_sequence,
    _UpdateFreeformViewUpdater,
)
from rumil.calls.stages import CallInfra, ContextResult
from rumil.constants import FREEFORM_VIEW_SECTIONS
from rumil.context import render_freeform_view
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
from rumil.views.freeform import FreeformView, create_freeform_view_for_question


def _question(headline: str = "Test question") -> Page:
    return Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=headline,
        headline=headline,
    )


async def _make_infra(tmp_db, q: Page, call_type: CallType) -> CallInfra:
    call = Call(
        call_type=call_type,
        workspace=Workspace.RESEARCH,
        scope_page_id=q.id,
        status=CallStatus.PENDING,
    )
    await tmp_db.save_call(call)
    return CallInfra(
        question_id=q.id,
        call=call,
        db=tmp_db,
        trace=CallTrace(call.id, tmp_db),
        state=MoveState(call, tmp_db),
    )


@pytest_asyncio.fixture
async def question(tmp_db):
    q = _question()
    await tmp_db.save_page(q)
    return q


async def test_create_materialize_creates_view_with_freeform_sections(tmp_db, question):
    """_CreateFreeformViewUpdater.materialize creates a VIEW page with the
    four freeform sections, view_kind=freeform metadata, and a VIEW_OF link."""
    view_id = str(uuid.uuid4())
    updater = _CreateFreeformViewUpdater(view_id, CallType.CREATE_FREEFORM_VIEW)
    infra = await _make_infra(tmp_db, question, CallType.CREATE_FREEFORM_VIEW)

    await updater.materialize(infra)

    view = await tmp_db.get_page(view_id)
    assert view is not None
    assert view.page_type == PageType.VIEW
    assert view.sections == list(FREEFORM_VIEW_SECTIONS)
    assert view.extra.get("view_kind") == VIEW_KIND_FREEFORM

    found_view = await tmp_db.get_view_for_question(question.id)
    assert found_view is not None
    assert found_view.id == view_id


async def test_create_materialize_refuses_when_view_exists(tmp_db, question):
    """Creating a freeform view should refuse if any view already exists."""
    existing = Page(
        page_type=PageType.VIEW,
        layer=PageLayer.WIKI,
        workspace=Workspace.RESEARCH,
        headline="View: Test question",
        content="",
        sections=list(FREEFORM_VIEW_SECTIONS),
    )
    await tmp_db.save_page(existing)
    await tmp_db.save_link(
        PageLink(
            from_page_id=existing.id,
            to_page_id=question.id,
            link_type=LinkType.VIEW_OF,
        )
    )

    updater = _CreateFreeformViewUpdater(str(uuid.uuid4()), CallType.CREATE_FREEFORM_VIEW)
    infra = await _make_infra(tmp_db, question, CallType.CREATE_FREEFORM_VIEW)
    with pytest.raises(ValueError, match="already exists"):
        await updater.materialize(infra)


async def test_update_materialize_supersedes_old_view_and_returns_prior_sections(tmp_db, question):
    """_UpdateFreeformViewUpdater.materialize should mint a fresh VIEW,
    supersede the old one, and return prior section content keyed by section name."""
    old_view = Page(
        id=str(uuid.uuid4()),
        page_type=PageType.VIEW,
        layer=PageLayer.WIKI,
        workspace=Workspace.RESEARCH,
        content="",
        headline="View: Test question",
        sections=list(FREEFORM_VIEW_SECTIONS),
        extra={"view_kind": VIEW_KIND_FREEFORM},
    )
    await tmp_db.save_page(old_view)
    await tmp_db.save_link(
        PageLink(
            from_page_id=old_view.id,
            to_page_id=question.id,
            link_type=LinkType.VIEW_OF,
        )
    )

    section_contents = {
        "framing_and_interpretation": "Old framing prose.",
        "assertions_and_deductions": "Old deductions prose.",
        "research_direction": "Old research-direction prose.",
        "returns_to_further_research": "Old returns-curve prose.",
    }
    for sec, content in section_contents.items():
        item = Page(
            page_type=PageType.VIEW_ITEM,
            layer=PageLayer.WIKI,
            workspace=Workspace.RESEARCH,
            headline=sec,
            content=content,
        )
        await tmp_db.save_page(item)
        await tmp_db.save_link(
            PageLink(
                from_page_id=old_view.id,
                to_page_id=item.id,
                link_type=LinkType.VIEW_ITEM,
                section=sec,
                position=0,
            )
        )

    new_view_id = str(uuid.uuid4())
    updater = _UpdateFreeformViewUpdater(new_view_id, CallType.UPDATE_FREEFORM_VIEW)
    infra = await _make_infra(tmp_db, question, CallType.UPDATE_FREEFORM_VIEW)
    old_id, prior_sections = await updater.materialize(infra)

    assert old_id == old_view.id
    assert prior_sections == section_contents

    # New view exists and is the active view for the question.
    found_view = await tmp_db.get_view_for_question(question.id)
    assert found_view is not None
    assert found_view.id == new_view_id
    assert found_view.extra.get("view_kind") == VIEW_KIND_FREEFORM

    # Old view is superseded.
    refreshed_old = await tmp_db.get_page(old_view.id)
    assert refreshed_old is not None
    assert refreshed_old.is_superseded


async def test_update_materialize_raises_without_existing_view(tmp_db, question):
    """Updating a freeform view should refuse when none exists."""
    updater = _UpdateFreeformViewUpdater(str(uuid.uuid4()), CallType.UPDATE_FREEFORM_VIEW)
    infra = await _make_infra(tmp_db, question, CallType.UPDATE_FREEFORM_VIEW)
    with pytest.raises(RuntimeError, match="requires an existing View"):
        await updater.materialize(infra)


async def test_run_section_sequence_creates_one_item_per_section(tmp_db, question, mocker):
    """_run_section_sequence should issue one LLM call per section,
    create one VIEW_ITEM per section with the right section metadata,
    and grow the messages list across calls."""
    calls_seen: list[dict] = []

    async def fake_text_call(system_prompt, **kwargs):
        msgs = kwargs.get("messages") or []
        calls_seen.append({"messages": list(msgs), "cache": kwargs.get("cache")})
        section = FREEFORM_VIEW_SECTIONS[len(calls_seen) - 1]
        return f"Prose for {section}."

    mocker.patch("rumil.calls.freeform_view.text_call", side_effect=fake_text_call)

    # Materialize the view first so VIEW_ITEM links have a valid parent.
    view_id = str(uuid.uuid4())
    create_updater = _CreateFreeformViewUpdater(view_id, CallType.CREATE_FREEFORM_VIEW)
    infra = await _make_infra(tmp_db, question, CallType.CREATE_FREEFORM_VIEW)
    await create_updater.materialize(infra)

    context = ContextResult(context_text="<context>", working_page_ids=[])
    result = await _run_section_sequence(infra, context, view_id=view_id, prior_sections=None)

    assert len(calls_seen) == len(FREEFORM_VIEW_SECTIONS)
    # Every call should request caching.
    assert all(c["cache"] is True for c in calls_seen)
    # Message history grows across calls (call N has 2*(N-1) prior messages).
    for i, c in enumerate(calls_seen):
        assert len(c["messages"]) == 2 * i + 1

    assert len(result.created_page_ids) == len(FREEFORM_VIEW_SECTIONS)
    items = await tmp_db.get_view_items(view_id)
    by_section = {link.section: page for page, link in items}
    for sec in FREEFORM_VIEW_SECTIONS:
        assert sec in by_section
        assert by_section[sec].content == f"Prose for {sec}."


async def test_run_section_sequence_includes_prior_version_when_updating(tmp_db, question, mocker):
    """When prior_sections is provided, each section's user message should
    contain the matching prior_version block."""
    seen_user_messages: list[str] = []

    async def fake_text_call(system_prompt, **kwargs):
        msgs = kwargs.get("messages") or []
        # Last user message is the most recent one we're about to respond to.
        last_user = next(
            (m for m in reversed(msgs) if m.get("role") == "user"),
            None,
        )
        seen_user_messages.append(last_user["content"] if last_user else "")
        section = FREEFORM_VIEW_SECTIONS[len(seen_user_messages) - 1]
        return f"Updated {section}."

    mocker.patch("rumil.calls.freeform_view.text_call", side_effect=fake_text_call)

    view_id = str(uuid.uuid4())
    create_updater = _CreateFreeformViewUpdater(view_id, CallType.CREATE_FREEFORM_VIEW)
    infra = await _make_infra(tmp_db, question, CallType.CREATE_FREEFORM_VIEW)
    await create_updater.materialize(infra)

    prior = {sec: f"PRIOR-{sec}-CONTENT" for sec in FREEFORM_VIEW_SECTIONS}
    context = ContextResult(context_text="<context>", working_page_ids=[])
    await _run_section_sequence(infra, context, view_id=view_id, prior_sections=prior)

    assert len(seen_user_messages) == len(FREEFORM_VIEW_SECTIONS)
    for sec, msg in zip(FREEFORM_VIEW_SECTIONS, seen_user_messages):
        assert "<prior_version>" in msg
        assert f"PRIOR-{sec}-CONTENT" in msg


async def test_freeform_view_refresh_routes_to_create_when_no_view(tmp_db, question, mocker):
    """FreeformView.refresh dispatches CreateFreeformView when no view exists."""
    create_mock = mocker.patch(
        "rumil.views.freeform.create_freeform_view_for_question",
        return_value="created-call-id",
    )
    update_mock = mocker.patch(
        "rumil.views.freeform.update_freeform_view_for_question",
        return_value="updated-call-id",
    )

    view = FreeformView()
    result = await view.refresh(question.id, tmp_db)
    assert result == "created-call-id"
    assert create_mock.called
    assert not update_mock.called


async def test_freeform_view_refresh_routes_to_update_when_view_exists(tmp_db, question, mocker):
    """FreeformView.refresh dispatches UpdateFreeformView when a view exists."""
    existing = Page(
        page_type=PageType.VIEW,
        layer=PageLayer.WIKI,
        workspace=Workspace.RESEARCH,
        headline="View: Test question",
        content="",
        sections=list(FREEFORM_VIEW_SECTIONS),
    )
    await tmp_db.save_page(existing)
    await tmp_db.save_link(
        PageLink(
            from_page_id=existing.id,
            to_page_id=question.id,
            link_type=LinkType.VIEW_OF,
        )
    )

    create_mock = mocker.patch(
        "rumil.views.freeform.create_freeform_view_for_question",
        return_value="created-call-id",
    )
    update_mock = mocker.patch(
        "rumil.views.freeform.update_freeform_view_for_question",
        return_value="updated-call-id",
    )

    view = FreeformView()
    result = await view.refresh(question.id, tmp_db)
    assert result == "updated-call-id"
    assert update_mock.called
    assert not create_mock.called


async def test_render_freeform_view_renders_sections_in_canonical_order():
    """render_freeform_view orders sections by view.sections, ignoring
    importance and rendering full content per section."""
    view = Page(
        page_type=PageType.VIEW,
        layer=PageLayer.WIKI,
        workspace=Workspace.RESEARCH,
        headline="View: Q",
        content="",
        sections=list(FREEFORM_VIEW_SECTIONS),
    )
    items: list[tuple[Page, PageLink]] = []
    # Insert items in REVERSE order to verify sorting by sections list.
    for sec in reversed(FREEFORM_VIEW_SECTIONS):
        item = Page(
            page_type=PageType.VIEW_ITEM,
            layer=PageLayer.WIKI,
            workspace=Workspace.RESEARCH,
            headline=sec,
            content=f"Body of {sec}.",
        )
        link = PageLink(
            from_page_id=view.id,
            to_page_id=item.id,
            link_type=LinkType.VIEW_ITEM,
            section=sec,
            position=0,
        )
        items.append((item, link))

    text = await render_freeform_view(view, items)

    # Each section header appears in canonical order.
    last_idx = -1
    for sec in FREEFORM_VIEW_SECTIONS:
        label = sec.replace("_", " ").title()
        idx = text.index(f"### {label}")
        assert idx > last_idx, f"section {sec} out of order"
        last_idx = idx
        assert f"Body of {sec}." in text


async def test_freeform_view_render_for_executive_summary_returns_none_without_view(
    tmp_db, question
):
    """render_for_executive_summary returns None when no view exists."""
    view = FreeformView()
    assert await view.render_for_executive_summary(question.id, tmp_db) is None


@pytest.mark.integration
async def test_freeform_view_create_end_to_end(tmp_db):
    """Real-LLM end-to-end: a CreateFreeformView call produces 1 view + 4 sections."""
    q = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="Will indoor agriculture be economically viable for staple crops by 2040?",
        headline="Will indoor agriculture be economically viable for staple crops by 2040?",
    )
    await tmp_db.save_page(q)

    call_id = await create_freeform_view_for_question(q.id, tmp_db, force=True)
    assert call_id is not None

    view = await tmp_db.get_view_for_question(q.id)
    assert view is not None
    assert view.extra.get("view_kind") == VIEW_KIND_FREEFORM

    items = await tmp_db.get_view_items(view.id)
    sections_seen = {link.section for _, link in items}
    assert sections_seen == set(FREEFORM_VIEW_SECTIONS)
    for page, _ in items:
        assert page.content.strip(), "each section should have non-empty prose"
