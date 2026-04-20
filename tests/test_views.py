"""Tests for src/rumil/views.py."""

import pytest
import pytest_asyncio

from rumil.models import (
    CallType,
    ConsiderationDirection,
    LinkRole,
    LinkType,
    Page,
    PageLayer,
    PageLink,
    PageType,
    Workspace,
)
from rumil.views import build_view


@pytest_asyncio.fixture
async def question(tmp_db):
    page = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="Why is the sky blue?",
        headline="Why is the sky blue?",
    )
    await tmp_db.save_page(page)
    return page


async def _make_claim(
    tmp_db,
    question,
    headline,
    *,
    credence=None,
    robustness=None,
    direction=ConsiderationDirection.SUPPORTS,
    role=LinkRole.DIRECT,
    strength=3.0,
    provenance_call_type="",
):
    claim = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=headline,
        headline=headline,
        credence=credence,
        robustness=robustness,
        provenance_call_type=provenance_call_type,
    )
    await tmp_db.save_page(claim)
    await tmp_db.save_link(
        PageLink(
            from_page_id=claim.id,
            to_page_id=question.id,
            link_type=LinkType.CONSIDERATION,
            strength=strength,
            direction=direction,
            role=role,
        )
    )
    return claim


async def _make_child_question(tmp_db, parent, headline, *, role=LinkRole.DIRECT):
    child = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=headline,
        headline=headline,
    )
    await tmp_db.save_page(child)
    await tmp_db.save_link(
        PageLink(
            from_page_id=parent.id,
            to_page_id=child.id,
            link_type=LinkType.CHILD_QUESTION,
            reasoning="Sub-question",
            role=role,
        )
    )
    return child


async def _make_judgement(tmp_db, question, headline, *, robustness=3):
    judgement = Page(
        page_type=PageType.JUDGEMENT,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=headline,
        headline=headline,
        robustness=robustness,
    )
    await tmp_db.save_page(judgement)
    await tmp_db.save_link(
        PageLink(
            from_page_id=judgement.id,
            to_page_id=question.id,
            link_type=LinkType.ANSWERS,
        )
    )
    return judgement


async def _make_stored_view(tmp_db, question):
    view = Page(
        page_type=PageType.VIEW,
        layer=PageLayer.WIKI,
        workspace=Workspace.RESEARCH,
        content="",
        headline=f"View: {question.headline}",
    )
    await tmp_db.save_page(view)
    await tmp_db.save_link(
        PageLink(
            from_page_id=view.id,
            to_page_id=question.id,
            link_type=LinkType.VIEW_OF,
        )
    )
    return view


async def _make_view_item(
    tmp_db,
    view,
    headline,
    *,
    section="key_evidence",
    importance=4,
    robustness=3,
    cites_page_ids=(),
):
    item = Page(
        page_type=PageType.VIEW_ITEM,
        layer=PageLayer.WIKI,
        workspace=Workspace.RESEARCH,
        content=headline,
        headline=headline,
        robustness=robustness,
    )
    await tmp_db.save_page(item)
    await tmp_db.save_link(
        PageLink(
            from_page_id=view.id,
            to_page_id=item.id,
            link_type=LinkType.VIEW_ITEM,
            importance=importance,
            section=section,
        )
    )
    for cited_id in cites_page_ids:
        await tmp_db.save_link(
            PageLink(
                from_page_id=item.id,
                to_page_id=cited_id,
                link_type=LinkType.CITES,
            )
        )
    return item


async def test_build_view_basic_structure(tmp_db, question):
    await _make_claim(tmp_db, question, "Rayleigh scattering", credence=7, robustness=4)
    await _make_child_question(tmp_db, question, "What wavelengths scatter most?")
    await _make_judgement(tmp_db, question, "Probably Rayleigh scattering")

    view = await build_view(tmp_db, question.id)

    assert view.question.id == question.id
    assert len(view.sections) > 0
    section_names = [s.name for s in view.sections]
    assert "assessments" in section_names
    assert view.health.total_pages > 0


async def test_assessments_contains_judgement(tmp_db, question):
    judgement = await _make_judgement(tmp_db, question, "Likely Rayleigh")

    view = await build_view(tmp_db, question.id)

    assessments = next(s for s in view.sections if s.name == "assessments")
    assert any(item.page.id == judgement.id for item in assessments.items)


async def test_confident_views_high_credence_high_robustness(tmp_db, question):
    claim = await _make_claim(
        tmp_db, question, "Well-established finding", credence=8, robustness=4
    )

    view = await build_view(tmp_db, question.id)

    confident = next(s for s in view.sections if s.name == "confident_views")
    assert [item.page.id for item in confident.items] == [claim.id]


async def test_live_hypotheses_low_robustness(tmp_db, question):
    claim = await _make_claim(tmp_db, question, "Fragile hypothesis", credence=7, robustness=2)

    view = await build_view(tmp_db, question.id)

    hyp = next(s for s in view.sections if s.name == "live_hypotheses")
    assert [item.page.id for item in hyp.items] == [claim.id]


async def test_live_hypotheses_mid_credence_no_robustness(tmp_db, question):
    claim = await _make_claim(tmp_db, question, "Uncertain claim", credence=5, robustness=None)

    view = await build_view(tmp_db, question.id)

    hyp = next(s for s in view.sections if s.name == "live_hypotheses")
    assert claim.id in {item.page.id for item in hyp.items}


async def test_key_evidence_cites_link(tmp_db, question):
    claim = await _make_claim(tmp_db, question, "Evidence-backed claim", credence=8, robustness=4)
    source = Page(
        page_type=PageType.SOURCE,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="A scientific paper",
        headline="Paper on scattering",
    )
    await tmp_db.save_page(source)
    await tmp_db.save_link(
        PageLink(
            from_page_id=claim.id,
            to_page_id=source.id,
            link_type=LinkType.CITES,
        )
    )

    view = await build_view(tmp_db, question.id)

    evidence = next(s for s in view.sections if s.name == "key_evidence")
    assert claim.id in {item.page.id for item in evidence.items}


async def test_key_evidence_ingest_provenance(tmp_db, question):
    claim = await _make_claim(
        tmp_db,
        question,
        "Ingested finding",
        credence=7,
        robustness=3,
        provenance_call_type=CallType.INGEST.value,
    )

    view = await build_view(tmp_db, question.id)

    evidence = next(s for s in view.sections if s.name == "key_evidence")
    assert claim.id in {item.page.id for item in evidence.items}


async def test_key_uncertainties_unjudged_child_question(tmp_db, question):
    child = await _make_child_question(tmp_db, question, "Unresolved sub-question")

    view = await build_view(tmp_db, question.id)

    uncert = next(s for s in view.sections if s.name == "key_uncertainties")
    assert [item.page.id for item in uncert.items] == [child.id]


async def test_key_uncertainties_mid_credence_robust(tmp_db, question):
    claim = await _make_claim(tmp_db, question, "Robust but uncertain", credence=5, robustness=4)

    view = await build_view(tmp_db, question.id)

    uncert = next(s for s in view.sections if s.name == "key_uncertainties")
    assert claim.id in {item.page.id for item in uncert.items}


async def test_broader_context_structural_claim(tmp_db, question):
    claim = await _make_claim(
        tmp_db,
        question,
        "Structural framing claim",
        credence=7,
        robustness=4,
        role=LinkRole.STRUCTURAL,
    )

    view = await build_view(tmp_db, question.id)

    ctx = next(s for s in view.sections if s.name == "broader_context")
    assert [item.page.id for item in ctx.items] == [claim.id]


async def test_broader_context_structural_child_question(tmp_db, question):
    child = await _make_child_question(
        tmp_db, question, "Structural sub-question", role=LinkRole.STRUCTURAL
    )

    view = await build_view(tmp_db, question.id)

    ctx = next(s for s in view.sections if s.name == "broader_context")
    assert [item.page.id for item in ctx.items] == [child.id]


async def test_child_question_with_judgement_goes_to_assessments(tmp_db, question):
    child = await _make_child_question(tmp_db, question, "Judged child")
    await _make_judgement(tmp_db, child, "Answer to child")

    view = await build_view(tmp_db, question.id)

    assessments = next(s for s in view.sections if s.name == "assessments")
    assert child.id in {item.page.id for item in assessments.items}


async def test_stored_view_items_included(tmp_db, question):
    stored = await _make_stored_view(tmp_db, question)
    item = await _make_view_item(
        tmp_db, stored, "Curated summary", section="confident_views", importance=5
    )

    view = await build_view(tmp_db, question.id)

    assert view.stored_view is not None
    assert view.stored_view.id == stored.id
    confident = next(s for s in view.sections if s.name == "confident_views")
    assert item.id in {i.page.id for i in confident.items}


async def test_graph_item_hidden_when_cited_by_view_item(tmp_db, question):
    claim = await _make_claim(
        tmp_db, question, "Underlying consideration", credence=8, robustness=4
    )
    stored = await _make_stored_view(tmp_db, question)
    await _make_view_item(
        tmp_db,
        stored,
        "Summary of consideration",
        section="confident_views",
        importance=5,
        cites_page_ids=[claim.id],
    )

    view = await build_view(tmp_db, question.id)

    all_item_page_ids = {item.page.id for s in view.sections for item in s.items}
    assert claim.id not in all_item_page_ids


async def test_graph_only_when_no_stored_view(tmp_db, question):
    claim = await _make_claim(tmp_db, question, "Raw consideration", credence=8, robustness=4)

    view = await build_view(tmp_db, question.id)

    assert view.stored_view is None
    all_item_page_ids = {item.page.id for s in view.sections for item in s.items}
    assert claim.id in all_item_page_ids


async def test_view_item_sorts_before_graph_at_same_importance(tmp_db, question):
    stored = await _make_stored_view(tmp_db, question)
    curated = await _make_view_item(
        tmp_db, stored, "Curated item", section="confident_views", importance=3
    )
    graph_claim = await _make_claim(
        tmp_db, question, "Graph claim", credence=8, robustness=4, strength=3.0
    )

    view = await build_view(tmp_db, question.id)

    confident = next(s for s in view.sections if s.name == "confident_views")
    ids_in_order = [item.page.id for item in confident.items]
    assert curated.id in ids_in_order
    assert graph_claim.id in ids_in_order
    assert ids_in_order.index(curated.id) < ids_in_order.index(graph_claim.id)


async def test_stored_view_item_uses_link_importance(tmp_db, question):
    stored = await _make_stored_view(tmp_db, question)
    item = await _make_view_item(tmp_db, stored, "Curated", section="confident_views", importance=5)

    view = await build_view(tmp_db, question.id)

    confident = next(s for s in view.sections if s.name == "confident_views")
    view_item = next(i for i in confident.items if i.page.id == item.id)
    assert view_item.effective_importance == 5


async def test_health_missing_credence(tmp_db, question):
    await _make_claim(tmp_db, question, "No credence", credence=None, robustness=3)

    view = await build_view(tmp_db, question.id)

    assert view.health.missing_credence == 1


async def test_health_child_questions_without_judgements(tmp_db, question):
    await _make_child_question(tmp_db, question, "Unjudged child 1")
    await _make_child_question(tmp_db, question, "Unjudged child 2")

    view = await build_view(tmp_db, question.id)

    assert view.health.child_questions_without_judgements == 2


async def test_health_judged_child_not_counted(tmp_db, question):
    child = await _make_child_question(tmp_db, question, "Judged child")
    await _make_judgement(tmp_db, child, "Answer to child")

    view = await build_view(tmp_db, question.id)

    assert view.health.child_questions_without_judgements == 0


async def test_superseded_pages_excluded(tmp_db, question):
    claim = await _make_claim(tmp_db, question, "Old superseded claim", credence=8, robustness=4)
    claim.is_superseded = True
    await tmp_db.save_page(claim)

    view = await build_view(tmp_db, question.id)

    all_page_ids = {item.page.id for s in view.sections for item in s.items}
    assert claim.id not in all_page_ids


async def test_build_view_raises_for_nonexistent_page(tmp_db):
    with pytest.raises(ValueError, match="not found"):
        await build_view(tmp_db, "00000000-0000-0000-0000-000000000000")


async def test_build_view_raises_for_non_question(tmp_db):
    claim = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="Not a question",
        headline="Not a question",
    )
    await tmp_db.save_page(claim)

    with pytest.raises(ValueError, match="not a question"):
        await build_view(tmp_db, claim.id)


async def test_max_depth_with_nested_children(tmp_db, question):
    child1 = await _make_child_question(tmp_db, question, "Level 1")
    await _make_child_question(tmp_db, child1, "Level 2")

    view = await build_view(tmp_db, question.id)

    assert view.health.max_depth == 2


async def test_max_depth_cycle_safe(tmp_db, question):
    child1 = await _make_child_question(tmp_db, question, "Level 1")
    await tmp_db.save_link(
        PageLink(
            from_page_id=child1.id,
            to_page_id=question.id,
            link_type=LinkType.CHILD_QUESTION,
        )
    )

    view = await build_view(tmp_db, question.id)

    assert view.health.max_depth < 20


async def test_sections_ordered_by_definition(tmp_db, question):
    await _make_child_question(tmp_db, question, "Unresolved child")
    await _make_claim(tmp_db, question, "Confident", credence=8, robustness=4)
    await _make_judgement(tmp_db, question, "Current answer")

    view = await build_view(tmp_db, question.id)

    section_names = [s.name for s in view.sections]
    expected_canonical = ["confident_views", "assessments", "key_uncertainties"]
    present_canonical = [n for n in expected_canonical if n in section_names]
    indices = [section_names.index(n) for n in present_canonical]
    assert indices == sorted(indices)


async def test_empty_view_no_research(tmp_db, question):
    view = await build_view(tmp_db, question.id)

    assert view.question.id == question.id
    assert len(view.sections) == 0
    assert view.health.total_pages == 0
