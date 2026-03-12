"""Test that project workspaces are properly isolated."""

import uuid

import pytest_asyncio

from differential.context import (
    assemble_call_context,
    build_context_for_question,
    build_prioritization_context,
)
from differential.database import DB
from differential.models import (
    ConsiderationDirection,
    LinkType,
    Page,
    PageLayer,
    PageLink,
    PageType,
    Workspace,
)
from differential.workspace_map import build_workspace_map


async def _make_db(project_name: str) -> DB:
    db = await DB.create(run_id=str(uuid.uuid4()))
    project = await db.get_or_create_project(project_name)
    db.project_id = project.id
    return db


async def _make_question(db: DB, text: str) -> Page:
    page = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=text,
        summary=text[:120],
    )
    await db.save_page(page)
    return page


async def _make_claim(db: DB, text: str) -> Page:
    page = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=text,
        summary=text[:120],
        epistemic_status=4.5,
        epistemic_type="well-established",
    )
    await db.save_page(page)
    return page


async def _link_consideration(
    db: DB, claim: Page, question: Page, direction: ConsiderationDirection
) -> None:
    await db.save_link(
        PageLink(
            from_page_id=claim.id,
            to_page_id=question.id,
            link_type=LinkType.CONSIDERATION,
            direction=direction,
            strength=4.0,
            reasoning="test link",
        )
    )


async def _make_source(db: DB, name: str) -> Page:
    page = Page(
        page_type=PageType.SOURCE,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=f"Content of {name}",
        summary=f"Source: {name}",
        extra={"filename": name, "char_count": 100},
    )
    await db.save_page(page)
    return page


@pytest_asyncio.fixture
async def two_workspaces():
    """Set up two isolated workspaces with questions, claims, and links."""
    db_alpha = await _make_db(f"alpha-{uuid.uuid4().hex[:8]}")
    db_beta = await _make_db(f"beta-{uuid.uuid4().hex[:8]}")

    q_alpha = await _make_question(db_alpha, "What colour is the sky?")
    claim_alpha = await _make_claim(
        db_alpha, "The sky is blue due to Rayleigh scattering"
    )
    await _link_consideration(
        db_alpha, claim_alpha, q_alpha, ConsiderationDirection.SUPPORTS
    )
    source_alpha = await _make_source(db_alpha, "sky-paper.pdf")

    q_beta = await _make_question(db_beta, "Why is the ocean salty?")
    claim_beta = await _make_claim(
        db_beta, "Rivers carry dissolved salts into the ocean"
    )
    await _link_consideration(
        db_beta, claim_beta, q_beta, ConsiderationDirection.SUPPORTS
    )

    yield {
        "db_alpha": db_alpha,
        "db_beta": db_beta,
        "q_alpha": q_alpha,
        "q_beta": q_beta,
        "claim_alpha": claim_alpha,
        "claim_beta": claim_beta,
        "source_alpha": source_alpha,
    }

    await db_alpha.delete_run_data(delete_project=True)
    await db_beta.delete_run_data(delete_project=True)


async def test_root_questions_isolated(two_workspaces):
    w = two_workspaces
    alpha_qs = await w["db_alpha"].get_root_questions()
    beta_qs = await w["db_beta"].get_root_questions()

    alpha_ids = {q.id for q in alpha_qs}
    beta_ids = {q.id for q in beta_qs}

    assert w["q_alpha"].id in alpha_ids
    assert w["q_beta"].id not in alpha_ids
    assert w["q_beta"].id in beta_ids
    assert w["q_alpha"].id not in beta_ids


async def test_get_pages_isolated(two_workspaces):
    w = two_workspaces
    alpha_pages = await w["db_alpha"].get_pages()
    beta_pages = await w["db_beta"].get_pages()

    alpha_ids = {p.id for p in alpha_pages}
    beta_ids = {p.id for p in beta_pages}

    assert w["claim_alpha"].id in alpha_ids
    assert w["claim_alpha"].id not in beta_ids
    assert w["claim_beta"].id in beta_ids
    assert w["claim_beta"].id not in alpha_ids


async def test_sources_isolated(two_workspaces):
    w = two_workspaces
    alpha_sources = await w["db_alpha"].get_pages(page_type=PageType.SOURCE)
    beta_sources = await w["db_beta"].get_pages(page_type=PageType.SOURCE)

    assert any(s.id == w["source_alpha"].id for s in alpha_sources)
    assert not any(s.id == w["source_alpha"].id for s in beta_sources)


async def test_workspace_map_isolated(two_workspaces):
    w = two_workspaces
    map_alpha, ids_alpha = await build_workspace_map(w["db_alpha"])
    map_beta, ids_beta = await build_workspace_map(w["db_beta"])

    assert w["q_alpha"].id[:8] in ids_alpha
    assert w["q_alpha"].id[:8] not in ids_beta
    assert w["q_beta"].id[:8] in ids_beta
    assert w["q_beta"].id[:8] not in ids_alpha

    assert "Rayleigh" in map_alpha
    assert "Rayleigh" not in map_beta
    assert "salty" in map_beta
    assert "salty" not in map_alpha


async def test_call_context_isolated(two_workspaces):
    w = two_workspaces
    wc_alpha, _ = await build_context_for_question(w["q_alpha"].id, w["db_alpha"])
    map_alpha, _ = await build_workspace_map(w["db_alpha"])
    ctx_alpha = assemble_call_context(wc_alpha, workspace_map=map_alpha)

    wc_beta, _ = await build_context_for_question(w["q_beta"].id, w["db_beta"])
    map_beta, _ = await build_workspace_map(w["db_beta"])
    ctx_beta = assemble_call_context(wc_beta, workspace_map=map_beta)

    assert "Rayleigh" in ctx_alpha
    assert "salty" not in ctx_alpha
    assert "salty" in ctx_beta
    assert "Rayleigh" not in ctx_beta


async def test_prioritization_context_isolated(two_workspaces):
    w = two_workspaces
    ctx_alpha, _ = await build_prioritization_context(
        w["db_alpha"], w["q_alpha"].id
    )
    ctx_beta, _ = await build_prioritization_context(
        w["db_beta"], w["q_beta"].id
    )

    assert "sky" in ctx_alpha.lower()
    assert "ocean" not in ctx_alpha.lower()
    assert "ocean" in ctx_beta.lower()
    assert "sky" not in ctx_beta.lower()


async def test_source_in_prioritization_context_isolated(two_workspaces):
    w = two_workspaces
    ctx_alpha, _ = await build_prioritization_context(
        w["db_alpha"], w["q_alpha"].id
    )
    ctx_beta, _ = await build_prioritization_context(
        w["db_beta"], w["q_beta"].id
    )

    assert "sky-paper.pdf" in ctx_alpha
    assert "sky-paper.pdf" not in ctx_beta
