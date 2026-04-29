"""Tests for the View ABC (lifecycle + variant registry)."""

import pytest

from rumil.models import LinkType, Page, PageLayer, PageLink, PageType, Workspace
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
    assert set(VIEW_VARIANTS) == {"sectioned", "judgement", "freeform"}


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


async def _save_judgement(db, question: Page, headline: str, robustness: int = 3) -> Page:
    judgement = Page(
        page_type=PageType.JUDGEMENT,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=f"Body for {headline}",
        headline=headline,
        robustness=robustness,
    )
    await db.save_page(judgement)
    await db.save_link(
        PageLink(
            from_page_id=judgement.id,
            to_page_id=question.id,
            link_type=LinkType.ANSWERS,
        )
    )
    return judgement


async def _save_view(db, question: Page, headline: str, robustness: int = 4) -> Page:
    view = Page(
        page_type=PageType.VIEW,
        layer=PageLayer.WIKI,
        workspace=Workspace.RESEARCH,
        content=f"Body for {headline}",
        headline=headline,
        sections=["broader_context", "confident_views"],
        robustness=robustness,
    )
    await db.save_page(view)
    await db.save_link(
        PageLink(
            from_page_id=view.id,
            to_page_id=question.id,
            link_type=LinkType.VIEW_OF,
        )
    )
    return view


async def test_judgement_view_headline_page_returns_latest_judgement(tmp_db, question_page):
    await _save_judgement(tmp_db, question_page, "Older take", robustness=2)
    latest = await _save_judgement(tmp_db, question_page, "Newer take", robustness=4)

    headline = await JudgementView().headline_page(question_page.id, tmp_db)

    assert headline is not None
    assert headline.id == latest.id


async def test_judgement_view_headline_page_returns_none_when_absent(tmp_db, question_page):
    headline = await JudgementView().headline_page(question_page.id, tmp_db)
    assert headline is None


async def test_sectioned_view_headline_page_returns_view(tmp_db, question_page):
    view = await _save_view(tmp_db, question_page, "View on Q")

    headline = await SectionedView().headline_page(question_page.id, tmp_db)

    assert headline is not None
    assert headline.id == view.id


async def test_sectioned_view_headline_page_returns_none_when_absent(tmp_db, question_page):
    headline = await SectionedView().headline_page(question_page.id, tmp_db)
    assert headline is None


async def test_judgement_view_headline_pages_many_batches(
    tmp_db, question_page, child_question_page
):
    judgement = await _save_judgement(tmp_db, child_question_page, "Child take")

    result = await JudgementView().headline_pages_many(
        [question_page.id, child_question_page.id], tmp_db
    )

    assert result[question_page.id] is None
    child_headline = result[child_question_page.id]
    assert child_headline is not None
    assert child_headline.id == judgement.id


async def test_sectioned_view_headline_pages_many_batches(
    tmp_db, question_page, child_question_page
):
    view = await _save_view(tmp_db, child_question_page, "View on child")

    result = await SectionedView().headline_pages_many(
        [question_page.id, child_question_page.id], tmp_db
    )

    assert result[question_page.id] is None
    child_headline = result[child_question_page.id]
    assert child_headline is not None
    assert child_headline.id == view.id


async def test_judgement_view_render_for_executive_summary(tmp_db, question_page):
    await _save_judgement(tmp_db, question_page, "The take", robustness=4)

    rendered = await JudgementView().render_for_executive_summary(question_page.id, tmp_db)

    assert rendered is not None
    assert "The take" in rendered


async def test_judgement_view_render_for_executive_summary_returns_none_when_absent(
    tmp_db, question_page
):
    rendered = await JudgementView().render_for_executive_summary(question_page.id, tmp_db)
    assert rendered is None


async def test_sectioned_view_render_for_executive_summary(tmp_db, question_page):
    await _save_view(tmp_db, question_page, "View headline")

    rendered = await SectionedView().render_for_executive_summary(question_page.id, tmp_db)

    assert rendered is not None
    assert "View headline" in rendered


async def test_sectioned_view_render_for_executive_summary_returns_none_when_absent(
    tmp_db, question_page
):
    rendered = await SectionedView().render_for_executive_summary(question_page.id, tmp_db)
    assert rendered is None
