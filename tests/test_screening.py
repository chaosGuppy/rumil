"""Tests for the screening candidate pool used by main-phase prioritization."""

from rumil.models import (
    CallType,
    LinkType,
    Page,
    PageLayer,
    PageLink,
    PageType,
    Workspace,
)
from rumil.orchestrators.screening import (
    ScreenCandidateKind,
    build_candidate_pool,
    candidates_from_scope_question,
    candidates_from_scouts,
    candidates_from_view,
    merge_provenance,
    scout_types_from,
)


async def _make_page(
    db,
    page_type: PageType,
    headline: str,
    *,
    robustness: int | None = None,
    credence: int | None = None,
) -> Page:
    page = Page(
        page_type=page_type,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=headline,
        headline=headline,
        robustness=robustness,
        credence=credence,
    )
    await db.save_page(page)
    return page


async def _link(db, from_page, to_page, link_type: LinkType, **kwargs) -> None:
    await db.save_link(
        PageLink(
            from_page_id=from_page.id if hasattr(from_page, "id") else from_page,
            to_page_id=to_page.id if hasattr(to_page, "id") else to_page,
            link_type=link_type,
            **kwargs,
        )
    )


def test_merge_provenance_preserves_order_and_dedupes():
    merged = merge_provenance(
        ["direct_subquestion", "cited_by:abc"],
        ["direct_subquestion", "cited_by:def"],
    )
    assert merged == ["direct_subquestion", "cited_by:abc", "cited_by:def"]


def test_scout_types_from_filters_non_scouts():
    selected = scout_types_from(
        [
            CallType.FIND_CONSIDERATIONS,
            CallType.SCOUT_HYPOTHESES,
            CallType.ASSESS,
            CallType.SCOUT_ANALOGIES,
            CallType.WEB_RESEARCH,
        ]
    )
    assert selected == [CallType.SCOUT_HYPOTHESES, CallType.SCOUT_ANALOGIES]


async def test_candidates_from_scouts_attaches_fruit(tmp_db, question_page):
    scout_types = [CallType.SCOUT_HYPOTHESES, CallType.SCOUT_ANALOGIES]
    candidates = await candidates_from_scouts(question_page.id, tmp_db, scout_types)

    assert {c.ref for c in candidates} == {"scout_hypotheses", "scout_analogies"}
    assert all(c.kind == ScreenCandidateKind.SCOUT for c in candidates)
    assert all(c.provenance == ["scout_type"] for c in candidates)
    # No scout calls have run yet, so last_fruit is None (unknown).
    assert all(c.signals["last_fruit"] is None for c in candidates)


async def test_candidates_from_scope_question_surfaces_subqs_and_considerations(
    tmp_db, question_page, child_question_page
):
    claim = await _make_page(tmp_db, PageType.CLAIM, "routine tasks will go first")
    await _link(tmp_db, claim, question_page, LinkType.CONSIDERATION)

    candidates = await candidates_from_scope_question(question_page.id, tmp_db)

    refs = {c.ref for c in candidates}
    assert child_question_page.id in refs
    assert claim.id in refs

    subq_cand = next(c for c in candidates if c.ref == child_question_page.id)
    assert subq_cand.kind == ScreenCandidateKind.PAGE
    assert subq_cand.provenance == ["direct_subquestion"]
    assert subq_cand.signals["page_type"] == "question"
    assert subq_cand.signals["has_own_view"] is False

    claim_cand = next(c for c in candidates if c.ref == claim.id)
    assert claim_cand.provenance == ["direct_consideration"]
    assert claim_cand.signals["page_type"] == "claim"


async def test_candidates_from_scope_question_flags_subqs_with_own_view(
    tmp_db, question_page, child_question_page
):
    view = await _make_page(tmp_db, PageType.VIEW, "subq view")
    await _link(tmp_db, view, child_question_page, LinkType.VIEW_OF)

    candidates = await candidates_from_scope_question(question_page.id, tmp_db)
    subq_cand = next(c for c in candidates if c.ref == child_question_page.id)
    assert subq_cand.signals["has_own_view"] is True


async def test_candidates_from_view_empty_without_view(tmp_db, question_page):
    assert await candidates_from_view(question_page.id, tmp_db) == []


async def test_candidates_from_view_includes_items_and_cited_claims(
    tmp_db, question_page
):
    view = await _make_page(tmp_db, PageType.VIEW, "view of Q")
    await _link(tmp_db, view, question_page, LinkType.VIEW_OF)

    item = await _make_page(tmp_db, PageType.VIEW_ITEM, "item 1", robustness=3)
    await _link(
        tmp_db,
        view,
        item,
        LinkType.VIEW_ITEM,
        importance=5,
        section="confident_views",
        position=0,
    )

    cited_claim = await _make_page(
        tmp_db, PageType.CLAIM, "cited claim", robustness=2, credence=7
    )
    await _link(tmp_db, item, cited_claim, LinkType.DEPENDS_ON)

    # Source citation — must NOT appear as a screenable candidate.
    source = await _make_page(tmp_db, PageType.SOURCE, "cited source")
    await _link(tmp_db, item, source, LinkType.CITES)

    candidates = await candidates_from_view(question_page.id, tmp_db)

    refs = {c.ref for c in candidates}
    assert item.id in refs
    assert cited_claim.id in refs
    assert source.id not in refs

    item_cand = next(c for c in candidates if c.ref == item.id)
    assert item_cand.signals["robustness"] == 3
    assert item_cand.signals["importance"] == 5
    assert item_cand.signals["section"] == "confident_views"
    assert item_cand.provenance == [f"view_item:{view.id}"]

    cited_cand = next(c for c in candidates if c.ref == cited_claim.id)
    assert cited_cand.provenance == [f"cited_by:{item.id}"]
    assert cited_cand.signals["page_type"] == "claim"
    assert cited_cand.signals["credence"] == 7


async def test_build_candidate_pool_dedupes_across_sources(
    tmp_db, question_page, child_question_page
):
    view = await _make_page(tmp_db, PageType.VIEW, "view")
    await _link(tmp_db, view, question_page, LinkType.VIEW_OF)

    item = await _make_page(tmp_db, PageType.VIEW_ITEM, "item")
    await _link(
        tmp_db,
        view,
        item,
        LinkType.VIEW_ITEM,
        importance=4,
        section="key_evidence",
        position=0,
    )
    # The view item cites the same subquestion that is a direct child of the
    # scope question — this is the dedup case we care about.
    await _link(tmp_db, item, child_question_page, LinkType.RELATED)

    pool = await build_candidate_pool(
        question_page.id,
        tmp_db,
        scout_types=[CallType.SCOUT_HYPOTHESES],
    )

    subq_cands = [c for c in pool if c.ref == child_question_page.id]
    assert len(subq_cands) == 1
    provenance = subq_cands[0].provenance
    assert f"cited_by:{item.id}" in provenance
    assert "direct_subquestion" in provenance

    # Scout candidate still present exactly once.
    scout_cands = [c for c in pool if c.ref == "scout_hypotheses"]
    assert len(scout_cands) == 1
    assert scout_cands[0].kind == ScreenCandidateKind.SCOUT

    # View item itself is in the pool.
    assert any(c.ref == item.id for c in pool)


async def test_build_candidate_pool_no_view_still_returns_scope_and_scouts(
    tmp_db, question_page, child_question_page
):
    pool = await build_candidate_pool(
        question_page.id,
        tmp_db,
        scout_types=[CallType.SCOUT_HYPOTHESES],
    )
    refs = {c.ref for c in pool}
    assert child_question_page.id in refs
    assert "scout_hypotheses" in refs
