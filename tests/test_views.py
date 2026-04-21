"""Tests for the View ABC (lifecycle + variant registry)."""

import pytest

from rumil.models import Page, PageLayer, PageType, Workspace
from rumil.settings import override_settings
from rumil.views import VIEW_VARIANTS, get_active_view
from rumil.views.judgement import JudgementView
from rumil.views.sectioned import SectionedView


@pytest.mark.parametrize(
    "variant,expected_cls",
    [("sectioned", SectionedView), ("judgement", JudgementView)],
)
def test_get_active_view_honors_setting(variant, expected_cls):
    with override_settings(view_variant=variant):
        view = get_active_view()
    assert isinstance(view, expected_cls)


def test_get_active_view_rejects_unknown_variant():
    with (
        override_settings(view_variant="nonsense"),
        pytest.raises(ValueError, match="Unknown view_variant"),
    ):
        get_active_view()


def test_view_variants_exposes_known_names():
    assert set(VIEW_VARIANTS) == {"sectioned", "judgement"}


async def test_sectioned_view_exists_reflects_db_state(tmp_db, question_page, mocker):
    """SectionedView.exists tracks whether the DB has an active view for the question."""
    view = SectionedView()

    assert await view.exists(question_page.id, tmp_db) is False

    fake_page = Page(
        page_type=PageType.VIEW,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="x",
        headline="view",
    )
    mocker.patch.object(tmp_db, "get_view_for_question", return_value=fake_page)
    assert await view.exists(question_page.id, tmp_db) is True


async def test_sectioned_view_refresh_creates_when_missing(tmp_db, question_page, mocker):
    """First refresh (no existing view) routes to the create-view path."""
    create_mock = mocker.patch(
        "rumil.views.sectioned.create_view_for_question",
        return_value="create-call-id",
    )
    update_mock = mocker.patch(
        "rumil.views.sectioned.update_view_for_question",
        return_value="update-call-id",
    )
    mocker.patch.object(tmp_db, "get_view_for_question", return_value=None)

    result = await SectionedView().refresh(question_page.id, tmp_db, force=True)

    assert result == "create-call-id"
    create_mock.assert_called_once()
    update_mock.assert_not_called()


async def test_sectioned_view_refresh_updates_when_present(tmp_db, question_page, mocker):
    """When a view already exists, refresh routes to the update-view path."""
    fake_page = Page(
        page_type=PageType.VIEW,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="x",
        headline="view",
    )
    create_mock = mocker.patch(
        "rumil.views.sectioned.create_view_for_question",
        return_value="create-call-id",
    )
    update_mock = mocker.patch(
        "rumil.views.sectioned.update_view_for_question",
        return_value="update-call-id",
    )
    mocker.patch.object(tmp_db, "get_view_for_question", return_value=fake_page)

    result = await SectionedView().refresh(question_page.id, tmp_db, force=True)

    assert result == "update-call-id"
    update_mock.assert_called_once()
    create_mock.assert_not_called()


async def test_judgement_view_refresh_calls_assess_with_summarise_false(
    tmp_db, question_page, mocker
):
    """JudgementView.refresh delegates to assess_question with summarise=False."""
    assess_mock = mocker.patch(
        "rumil.views.judgement.assess_question",
        return_value="judgement-call-id",
    )

    result = await JudgementView().refresh(question_page.id, tmp_db, force=True)

    assert result == "judgement-call-id"
    assess_mock.assert_called_once()
    assert assess_mock.call_args.kwargs["summarise"] is False
