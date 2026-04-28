"""Tests for the run-eval seed context builder."""

import pytest

from rumil.models import (
    LinkType,
    Page,
    PageLayer,
    PageLink,
    PageType,
    Workspace,
)
from rumil.run_eval.seed import build_eval_seed_context
from rumil.settings import override_settings


def _question(headline: str) -> Page:
    return Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=f"Full content for {headline}",
        headline=headline,
    )


def _judgement(headline: str) -> Page:
    return Page(
        page_type=PageType.JUDGEMENT,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=f"Full content for {headline}",
        headline=headline,
        robustness=3,
    )


def _view(headline: str) -> Page:
    return Page(
        page_type=PageType.VIEW,
        layer=PageLayer.WIKI,
        workspace=Workspace.RESEARCH,
        content=f"Full content for {headline}",
        headline=headline,
        sections=["broader_context", "confident_views"],
        robustness=3,
    )


async def _link(db, parent: Page, child: Page, link_type: LinkType, run_id: str = "") -> None:
    await db.save_link(
        PageLink(
            from_page_id=parent.id,
            to_page_id=child.id,
            link_type=link_type,
            run_id=run_id,
        )
    )


@pytest.mark.asyncio
async def test_seed_renders_scope_full_content(tmp_db):
    scope = _question("Root scope")
    await tmp_db.save_page(scope)

    seed = await build_eval_seed_context(scope.id, tmp_db)

    assert "## Scope question" in seed
    assert "Root scope" in seed
    assert "Full content for Root scope" in seed


@pytest.mark.asyncio
async def test_seed_includes_current_take_full_content_judgement_variant(tmp_db):
    scope = _question("Root scope")
    judgement = _judgement("The answer")
    await tmp_db.save_page(scope)
    await tmp_db.save_page(judgement)
    await _link(tmp_db, judgement, scope, LinkType.ANSWERS)

    with override_settings(view_variant="judgement"):
        seed = await build_eval_seed_context(scope.id, tmp_db)

    assert "## Current take" in seed
    assert "Full content for The answer" in seed


@pytest.mark.asyncio
async def test_seed_includes_current_take_full_content_sectioned_variant(tmp_db):
    scope = _question("Root scope")
    view = _view("View on root scope")
    await tmp_db.save_page(scope)
    await tmp_db.save_page(view)
    await _link(tmp_db, view, scope, LinkType.VIEW_OF)

    with override_settings(view_variant="sectioned"):
        seed = await build_eval_seed_context(scope.id, tmp_db)

    assert "## Current take" in seed
    assert "View on root scope" in seed
    assert "Full content for View on root scope" in seed


@pytest.mark.asyncio
async def test_seed_omits_take_section_when_no_take_recorded(tmp_db):
    scope = _question("Root scope")
    await tmp_db.save_page(scope)

    seed = await build_eval_seed_context(scope.id, tmp_db)

    assert "## Current take" not in seed


@pytest.mark.asyncio
async def test_seed_renders_one_hop_neighbors_as_headlines_only(tmp_db):
    scope = _question("Root scope")
    child = _question("Child Q")
    await tmp_db.save_page(scope)
    await tmp_db.save_page(child)
    await _link(tmp_db, scope, child, LinkType.CHILD_QUESTION)

    seed = await build_eval_seed_context(scope.id, tmp_db)

    assert "## Local subgraph" in seed
    assert child.id[:8] in seed
    assert "Child Q" in seed
    # The child should only appear at headline level -- its full content
    # should not be present.
    assert "Full content for Child Q" not in seed


@pytest.mark.asyncio
async def test_seed_shows_overflow_marker_beyond_one_hop(tmp_db):
    scope = _question("Root scope")
    child = _question("Child Q")
    grandchild = _question("Grandchild Q")
    await tmp_db.save_page(scope)
    await tmp_db.save_page(child)
    await tmp_db.save_page(grandchild)
    await _link(tmp_db, scope, child, LinkType.CHILD_QUESTION)
    await _link(tmp_db, child, grandchild, LinkType.CHILD_QUESTION)

    seed = await build_eval_seed_context(scope.id, tmp_db)

    assert child.id[:8] in seed
    assert grandchild.id[:8] not in seed
    assert "not shown -- horizon" in seed


@pytest.mark.asyncio
async def test_seed_excludes_judgement_headline_from_subgraph(tmp_db):
    scope = _question("Root scope")
    judgement = _judgement("The answer")
    await tmp_db.save_page(scope)
    await tmp_db.save_page(judgement)
    await _link(tmp_db, judgement, scope, LinkType.ANSWERS)

    with override_settings(view_variant="judgement"):
        seed = await build_eval_seed_context(scope.id, tmp_db)

    # Full content in the take section, not duplicated as a headline
    # entry in the 1-hop subgraph.
    subgraph_section = seed.split("## Local subgraph", 1)[1]
    assert judgement.id[:8] not in subgraph_section


@pytest.mark.asyncio
async def test_seed_excludes_view_headline_from_subgraph(tmp_db):
    scope = _question("Root scope")
    view = _view("View on root scope")
    await tmp_db.save_page(scope)
    await tmp_db.save_page(view)
    await _link(tmp_db, view, scope, LinkType.VIEW_OF)

    with override_settings(view_variant="sectioned"):
        seed = await build_eval_seed_context(scope.id, tmp_db)

    subgraph_section = seed.split("## Local subgraph", 1)[1]
    assert view.id[:8] not in subgraph_section


@pytest.mark.asyncio
async def test_seed_marks_links_added_by_run(tmp_db):
    scope = _question("Root scope")
    child = _question("Child Q")
    await tmp_db.save_page(scope)
    await tmp_db.save_page(child)
    # save_link tags each link with the DB's run_id, so highlighting against
    # that run_id surfaces the freshly-created link as "LINKED BY THIS RUN".
    await _link(tmp_db, scope, child, LinkType.CHILD_QUESTION)

    seed = await build_eval_seed_context(scope.id, tmp_db, highlight_run_id=tmp_db.run_id)

    assert "LINKED BY THIS RUN" in seed


@pytest.mark.asyncio
async def test_seed_handles_unknown_page_id(tmp_db):
    seed = await build_eval_seed_context("deadbeef", tmp_db)

    assert "not found" in seed
