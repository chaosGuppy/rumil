"""Tests for default workspace lifecycle hooks (`rumil/hooks.py`).

Mocks `create_view_for_question` at the module boundary so these tests stay
fast and don't call the LLM — we're testing the handler's gating logic, not
the quality of the View it eventually produces. A separate LLM-tagged test
would be the right place to check end-to-end View creation.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from rumil.events import PageCreatedEvent
from rumil.hooks import auto_create_view_on_question
from rumil.models import Page, PageLayer, PageType, Workspace
from rumil.settings import override_settings


@pytest_asyncio.fixture
async def new_question(tmp_db):
    """A freshly-saved question page with no View attached."""
    page = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="Will frontier AI automate routine cognitive labour by 2030?",
        headline="Will frontier AI automate routine cognitive labour by 2030?",
    )
    await tmp_db.save_page(page)
    return page


async def test_handler_creates_view_for_question_when_enabled(tmp_db, new_question, mocker):
    spy = mocker.patch(
        "rumil.orchestrators.common.create_view_for_question",
        return_value="fake-call-id",
    )
    event = PageCreatedEvent(
        page_id=new_question.id,
        page_type=PageType.QUESTION,
        run_id=tmp_db.run_id,
        staged=tmp_db.staged,
        db=tmp_db,
    )
    with override_settings(auto_create_view_on_question=True):
        await auto_create_view_on_question(event)

    spy.assert_called_once()
    args, _ = spy.call_args
    assert args[0] == new_question.id
    assert args[1] is tmp_db


async def test_handler_noop_when_setting_off(tmp_db, new_question, mocker):
    spy = mocker.patch("rumil.orchestrators.common.create_view_for_question")
    event = PageCreatedEvent(
        page_id=new_question.id,
        page_type=PageType.QUESTION,
        run_id=tmp_db.run_id,
        staged=tmp_db.staged,
        db=tmp_db,
    )
    with override_settings(auto_create_view_on_question=False):
        await auto_create_view_on_question(event)

    spy.assert_not_called()


@pytest.mark.parametrize(
    "page_type",
    [PageType.CLAIM, PageType.JUDGEMENT, PageType.VIEW, PageType.VIEW_ITEM],
)
async def test_handler_noop_for_non_question_page_types(tmp_db, mocker, page_type):
    spy = mocker.patch("rumil.orchestrators.common.create_view_for_question")
    event = PageCreatedEvent(
        page_id="irrelevant",
        page_type=page_type,
        run_id=tmp_db.run_id,
        staged=tmp_db.staged,
        db=tmp_db,
    )
    with override_settings(auto_create_view_on_question=True):
        await auto_create_view_on_question(event)

    spy.assert_not_called()


async def test_handler_noop_when_db_is_none(mocker):
    spy = mocker.patch("rumil.orchestrators.common.create_view_for_question")
    event = PageCreatedEvent(
        page_id="q1",
        page_type=PageType.QUESTION,
        run_id="r1",
        staged=False,
        db=None,
    )
    with override_settings(auto_create_view_on_question=True):
        await auto_create_view_on_question(event)

    spy.assert_not_called()


async def test_handler_noop_when_view_already_exists(tmp_db, new_question, mocker):
    """If a View already exists for the question, don't create a second one —
    the orchestrator's later UpdateView dispatches would otherwise need to
    reason about which of two Views to refine.
    """
    existing_view = Page(
        page_type=PageType.VIEW,
        layer=PageLayer.WIKI,
        workspace=Workspace.RESEARCH,
        content="Pre-existing synthesis.",
        headline="Existing View",
    )
    await tmp_db.save_page(existing_view)
    from rumil.models import LinkType, PageLink

    await tmp_db.save_link(
        PageLink(
            from_page_id=existing_view.id,
            to_page_id=new_question.id,
            link_type=LinkType.VIEW_OF,
            reasoning="seed",
        )
    )

    spy = mocker.patch("rumil.orchestrators.common.create_view_for_question")
    event = PageCreatedEvent(
        page_id=new_question.id,
        page_type=PageType.QUESTION,
        run_id=tmp_db.run_id,
        staged=tmp_db.staged,
        db=tmp_db,
    )
    with override_settings(auto_create_view_on_question=True):
        await auto_create_view_on_question(event)

    spy.assert_not_called()
