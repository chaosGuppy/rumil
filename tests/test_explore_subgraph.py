"""Tests for subgraph rendering — verifying that the 'Answered at robustness'
annotation flows through the active View variant rather than judgements only.
"""

import pytest

from rumil.models import LinkType, Page, PageLayer, PageLink, PageType, Workspace
from rumil.settings import override_settings
from rumil.workspace_exploration.explore import render_subgraph


def _question(headline: str) -> Page:
    return Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=f"Body: {headline}",
        headline=headline,
    )


def _judgement(headline: str, robustness: int) -> Page:
    return Page(
        page_type=PageType.JUDGEMENT,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=f"Body: {headline}",
        headline=headline,
        robustness=robustness,
    )


def _view(headline: str, robustness: int) -> Page:
    return Page(
        page_type=PageType.VIEW,
        layer=PageLayer.WIKI,
        workspace=Workspace.RESEARCH,
        content=f"Body: {headline}",
        headline=headline,
        sections=["confident_views"],
        robustness=robustness,
    )


@pytest.mark.asyncio
async def test_subgraph_marks_question_with_view_as_answered_under_sectioned_variant(tmp_db):
    """A question with a View (and no judgement) should show 'Answered at robustness ...'."""
    parent = _question("Parent Q")
    child = _question("Child Q")
    view = _view("View on child", robustness=4)
    await tmp_db.save_page(parent)
    await tmp_db.save_page(child)
    await tmp_db.save_page(view)
    await tmp_db.save_link(
        PageLink(
            from_page_id=parent.id,
            to_page_id=child.id,
            link_type=LinkType.CHILD_QUESTION,
        )
    )
    await tmp_db.save_link(
        PageLink(
            from_page_id=view.id,
            to_page_id=child.id,
            link_type=LinkType.VIEW_OF,
        )
    )

    with override_settings(view_variant="sectioned"):
        result = await render_subgraph(parent.id, tmp_db, max_depth=1)

    assert "Answered at robustness 4/5" in result


@pytest.mark.asyncio
async def test_subgraph_marks_question_with_judgement_as_answered_under_judgement_variant(
    tmp_db,
):
    parent = _question("Parent Q")
    child = _question("Child Q")
    judgement = _judgement("Judgement on child", robustness=3)
    await tmp_db.save_page(parent)
    await tmp_db.save_page(child)
    await tmp_db.save_page(judgement)
    await tmp_db.save_link(
        PageLink(
            from_page_id=parent.id,
            to_page_id=child.id,
            link_type=LinkType.CHILD_QUESTION,
        )
    )
    await tmp_db.save_link(
        PageLink(
            from_page_id=judgement.id,
            to_page_id=child.id,
            link_type=LinkType.ANSWERS,
        )
    )

    with override_settings(view_variant="judgement"):
        result = await render_subgraph(parent.id, tmp_db, max_depth=1)

    assert "Answered at robustness 3/5" in result


@pytest.mark.asyncio
async def test_subgraph_marks_unanswered_question(tmp_db):
    parent = _question("Parent Q")
    child = _question("Child Q")
    await tmp_db.save_page(parent)
    await tmp_db.save_page(child)
    await tmp_db.save_link(
        PageLink(
            from_page_id=parent.id,
            to_page_id=child.id,
            link_type=LinkType.CHILD_QUESTION,
        )
    )

    result = await render_subgraph(parent.id, tmp_db, max_depth=1)

    assert "Unanswered" in result
