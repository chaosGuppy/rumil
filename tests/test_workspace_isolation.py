"""Test that project workspaces are properly isolated."""

import uuid

from differential.context import build_call_context, build_prioritization_context
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


def _make_db(project_name: str) -> DB:
    db = DB(run_id=str(uuid.uuid4()))
    project = db.get_or_create_project(project_name)
    db.project_id = project.id
    return db


def _make_question(db: DB, text: str) -> Page:
    page = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=text,
        summary=text[:120],
    )
    db.save_page(page)
    return page


def _make_claim(db: DB, text: str) -> Page:
    page = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=text,
        summary=text[:120],
        epistemic_status=4.5,
        epistemic_type="well-established",
    )
    db.save_page(page)
    return page


def _link_consideration(
    db: DB, claim: Page, question: Page, direction: ConsiderationDirection
) -> None:
    db.save_link(
        PageLink(
            from_page_id=claim.id,
            to_page_id=question.id,
            link_type=LinkType.CONSIDERATION,
            direction=direction,
            strength=4.0,
            reasoning="test link",
        )
    )


def _make_source(db: DB, name: str) -> Page:
    page = Page(
        page_type=PageType.SOURCE,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=f"Content of {name}",
        summary=f"Source: {name}",
        extra={"filename": name, "char_count": 100},
    )
    db.save_page(page)
    return page


class TestWorkspaceIsolation:
    """Pages and calls in one project must not leak into another project's views."""

    def setup_method(self):
        self.db_alpha = _make_db(f"alpha-{uuid.uuid4().hex[:8]}")
        self.db_beta = _make_db(f"beta-{uuid.uuid4().hex[:8]}")

        self.q_alpha = _make_question(self.db_alpha, "What colour is the sky?")
        self.claim_alpha = _make_claim(
            self.db_alpha, "The sky is blue due to Rayleigh scattering"
        )
        _link_consideration(
            self.db_alpha,
            self.claim_alpha,
            self.q_alpha,
            ConsiderationDirection.SUPPORTS,
        )
        self.source_alpha = _make_source(self.db_alpha, "sky-paper.pdf")

        self.q_beta = _make_question(self.db_beta, "Why is the ocean salty?")
        self.claim_beta = _make_claim(
            self.db_beta, "Rivers carry dissolved salts into the ocean"
        )
        _link_consideration(
            self.db_beta,
            self.claim_beta,
            self.q_beta,
            ConsiderationDirection.SUPPORTS,
        )

    def teardown_method(self):
        self.db_alpha.delete_run_data(delete_project=True)
        self.db_beta.delete_run_data(delete_project=True)

    def test_root_questions_isolated(self):
        alpha_qs = self.db_alpha.get_root_questions()
        beta_qs = self.db_beta.get_root_questions()

        alpha_ids = {q.id for q in alpha_qs}
        beta_ids = {q.id for q in beta_qs}

        assert self.q_alpha.id in alpha_ids
        assert self.q_beta.id not in alpha_ids
        assert self.q_beta.id in beta_ids
        assert self.q_alpha.id not in beta_ids

    def test_get_pages_isolated(self):
        alpha_pages = self.db_alpha.get_pages()
        beta_pages = self.db_beta.get_pages()

        alpha_ids = {p.id for p in alpha_pages}
        beta_ids = {p.id for p in beta_pages}

        assert self.claim_alpha.id in alpha_ids
        assert self.claim_alpha.id not in beta_ids
        assert self.claim_beta.id in beta_ids
        assert self.claim_beta.id not in alpha_ids

    def test_sources_isolated(self):
        alpha_sources = self.db_alpha.get_pages(page_type=PageType.SOURCE)
        beta_sources = self.db_beta.get_pages(page_type=PageType.SOURCE)

        assert any(s.id == self.source_alpha.id for s in alpha_sources)
        assert not any(s.id == self.source_alpha.id for s in beta_sources)

    def test_workspace_map_isolated(self):
        map_alpha, ids_alpha = build_workspace_map(self.db_alpha)
        map_beta, ids_beta = build_workspace_map(self.db_beta)

        assert self.q_alpha.id[:8] in ids_alpha
        assert self.q_alpha.id[:8] not in ids_beta
        assert self.q_beta.id[:8] in ids_beta
        assert self.q_beta.id[:8] not in ids_alpha

        assert "Rayleigh" in map_alpha
        assert "Rayleigh" not in map_beta
        assert "salty" in map_beta
        assert "salty" not in map_alpha

    def test_call_context_isolated(self):
        ctx_alpha, _, _ = build_call_context(self.q_alpha.id, self.db_alpha)
        ctx_beta, _, _ = build_call_context(self.q_beta.id, self.db_beta)

        assert "Rayleigh" in ctx_alpha
        assert "salty" not in ctx_alpha
        assert "salty" in ctx_beta
        assert "Rayleigh" not in ctx_beta

    def test_prioritization_context_isolated(self):
        ctx_alpha, _ = build_prioritization_context(
            self.db_alpha, self.q_alpha.id
        )
        ctx_beta, _ = build_prioritization_context(
            self.db_beta, self.q_beta.id
        )

        assert "sky" in ctx_alpha.lower()
        assert "ocean" not in ctx_alpha.lower()
        assert "ocean" in ctx_beta.lower()
        assert "sky" not in ctx_beta.lower()

    def test_source_in_prioritization_context_isolated(self):
        ctx_alpha, _ = build_prioritization_context(
            self.db_alpha, self.q_alpha.id
        )
        ctx_beta, _ = build_prioritization_context(
            self.db_beta, self.q_beta.id
        )

        assert "sky-paper.pdf" in ctx_alpha
        assert "sky-paper.pdf" not in ctx_beta
