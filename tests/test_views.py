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
from rumil.views import build_view, render_view_as_context


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
    importance=None,
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
        importance=importance,
        provenance_call_type=provenance_call_type,
    )
    await tmp_db.save_page(claim)
    await tmp_db.save_link(PageLink(
        from_page_id=claim.id,
        to_page_id=question.id,
        link_type=LinkType.CONSIDERATION,
        strength=strength,
        direction=direction,
        role=role,
    ))
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
    await tmp_db.save_link(PageLink(
        from_page_id=parent.id,
        to_page_id=child.id,
        link_type=LinkType.CHILD_QUESTION,
        reasoning="Sub-question",
        role=role,
    ))
    return child


async def _make_judgement(tmp_db, question, headline):
    judgement = Page(
        page_type=PageType.JUDGEMENT,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=headline,
        headline=headline,
    )
    await tmp_db.save_page(judgement)
    await tmp_db.save_link(PageLink(
        from_page_id=judgement.id,
        to_page_id=question.id,
        link_type=LinkType.ANSWERS,
    ))
    return judgement


async def test_build_view_basic_structure(tmp_db, question):
    await _make_claim(
        tmp_db, question, "Rayleigh scattering",
        credence=7, robustness=4, importance=0,
    )
    child = await _make_child_question(tmp_db, question, "What wavelengths scatter most?")
    await _make_judgement(tmp_db, question, "Probably Rayleigh scattering")

    view = await build_view(tmp_db, question.id)

    assert view.question.id == question.id
    assert len(view.sections) > 0
    section_names = [s.name for s in view.sections]
    assert "current_position" in section_names
    assert "core_findings" in section_names
    assert view.health.total_pages > 0


async def test_core_findings_high_credence_low_importance(tmp_db, question):
    await _make_claim(
        tmp_db, question, "Well-established finding",
        credence=7, robustness=4, importance=0,
    )

    view = await build_view(tmp_db, question.id)

    core = next(s for s in view.sections if s.name == "core_findings")
    assert len(core.items) == 1
    assert core.items[0].page.headline == "Well-established finding"


async def test_live_hypotheses_low_robustness(tmp_db, question):
    await _make_claim(
        tmp_db, question, "Fragile hypothesis",
        credence=7, robustness=2, importance=1,
    )

    view = await build_view(tmp_db, question.id)

    hyp = next(s for s in view.sections if s.name == "live_hypotheses")
    assert len(hyp.items) == 1
    assert hyp.items[0].page.headline == "Fragile hypothesis"


async def test_live_hypotheses_mid_credence(tmp_db, question):
    await _make_claim(
        tmp_db, question, "Uncertain claim",
        credence=5, robustness=3, importance=2,
    )

    view = await build_view(tmp_db, question.id)

    hyp = next(s for s in view.sections if s.name == "live_hypotheses")
    assert len(hyp.items) == 1
    assert hyp.items[0].page.headline == "Uncertain claim"


async def test_key_evidence_cites_link(tmp_db, question):
    claim = await _make_claim(
        tmp_db, question, "Evidence-backed claim",
        credence=8, robustness=4, importance=0,
    )
    source = Page(
        page_type=PageType.SOURCE,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="A scientific paper",
        headline="Paper on scattering",
    )
    await tmp_db.save_page(source)
    await tmp_db.save_link(PageLink(
        from_page_id=claim.id,
        to_page_id=source.id,
        link_type=LinkType.CITES,
    ))

    view = await build_view(tmp_db, question.id)

    evidence = next(s for s in view.sections if s.name == "key_evidence")
    evidence_ids = {item.page.id for item in evidence.items}
    assert claim.id in evidence_ids


async def test_key_evidence_ingest_provenance(tmp_db, question):
    await _make_claim(
        tmp_db, question, "Ingested finding",
        credence=7, robustness=3, importance=1,
        provenance_call_type=CallType.INGEST.value,
    )

    view = await build_view(tmp_db, question.id)

    evidence = next(s for s in view.sections if s.name == "key_evidence")
    assert len(evidence.items) >= 1
    assert any(i.page.headline == "Ingested finding" for i in evidence.items)


async def test_key_uncertainties_child_question_no_judgement(tmp_db, question):
    child = await _make_child_question(tmp_db, question, "Unresolved sub-question")

    view = await build_view(tmp_db, question.id)

    uncert = next(s for s in view.sections if s.name == "key_uncertainties")
    assert len(uncert.items) == 1
    assert uncert.items[0].page.headline == "Unresolved sub-question"


async def test_structural_framing_role(tmp_db, question):
    await _make_claim(
        tmp_db, question, "Structural framing claim",
        credence=7, robustness=4, importance=0,
        role=LinkRole.STRUCTURAL,
    )

    view = await build_view(tmp_db, question.id)

    structural = next(s for s in view.sections if s.name == "structural_framing")
    assert len(structural.items) == 1
    assert structural.items[0].page.headline == "Structural framing claim"


async def test_structural_framing_child_question(tmp_db, question):
    await _make_child_question(
        tmp_db, question, "Structural sub-question", role=LinkRole.STRUCTURAL,
    )

    view = await build_view(tmp_db, question.id)

    structural = next(s for s in view.sections if s.name == "structural_framing")
    assert len(structural.items) == 1
    assert structural.items[0].page.headline == "Structural sub-question"


async def test_promotion_candidates(tmp_db, question):
    await _make_claim(
        tmp_db, question, "Promote me",
        credence=8, robustness=4, importance=2,
    )

    view = await build_view(tmp_db, question.id)

    promo = next(s for s in view.sections if s.name == "promotion_candidates")
    assert len(promo.items) == 1
    assert promo.items[0].page.headline == "Promote me"


async def test_demotion_candidates(tmp_db, question):
    await _make_claim(
        tmp_db, question, "Demote me",
        credence=7, robustness=1, importance=0,
    )

    view = await build_view(tmp_db, question.id)

    demo = next(s for s in view.sections if s.name == "demotion_candidates")
    assert len(demo.items) == 1
    assert demo.items[0].page.headline == "Demote me"


async def test_health_missing_credence(tmp_db, question):
    await _make_claim(
        tmp_db, question, "No credence",
        credence=None, robustness=3, importance=1,
    )

    view = await build_view(tmp_db, question.id)

    assert view.health.missing_credence == 1


async def test_health_missing_importance(tmp_db, question):
    await _make_claim(
        tmp_db, question, "No importance",
        credence=7, robustness=3, importance=None,
    )

    view = await build_view(tmp_db, question.id)

    assert view.health.missing_importance == 1


async def test_health_child_questions_without_judgements(tmp_db, question):
    await _make_child_question(tmp_db, question, "Unjudged child 1")
    await _make_child_question(tmp_db, question, "Unjudged child 2")

    view = await build_view(tmp_db, question.id)

    assert view.health.child_questions_without_judgements == 2


async def test_health_child_with_judgement_not_counted(tmp_db, question):
    child = await _make_child_question(tmp_db, question, "Judged child")
    await _make_judgement(tmp_db, child, "Answer to child")

    view = await build_view(tmp_db, question.id)

    assert view.health.child_questions_without_judgements == 0


async def test_render_view_as_context_basic(tmp_db, question):
    await _make_claim(
        tmp_db, question, "A core finding",
        credence=8, robustness=4, importance=0,
    )
    await _make_judgement(tmp_db, question, "Current answer")

    view = await build_view(tmp_db, question.id)
    rendered = render_view_as_context(view)

    assert isinstance(rendered, str)
    assert "Why is the sky blue?" in rendered
    assert "Core Findings" in rendered
    assert "Current Position" in rendered
    assert "A core finding" in rendered


async def test_render_view_respects_char_budget(tmp_db, question):
    for i in range(20):
        await _make_claim(
            tmp_db, question, f"Verbose claim number {i} with lots of detail",
            credence=7, robustness=3, importance=1,
        )

    view = await build_view(tmp_db, question.id)
    rendered = render_view_as_context(view, char_budget=500)

    assert len(rendered) < 700


async def test_render_view_empty_question(tmp_db, question):
    view = await build_view(tmp_db, question.id)
    rendered = render_view_as_context(view)

    assert "No research yet" in rendered


async def test_empty_view_no_research(tmp_db, question):
    view = await build_view(tmp_db, question.id)

    assert view.question.id == question.id
    assert len(view.sections) == 0
    assert view.health.total_pages == 0


async def test_importance_threshold_filters_supporting_detail(tmp_db, question):
    await _make_claim(
        tmp_db, question, "Low-priority claim",
        credence=3, robustness=3, importance=4,
    )

    view_broad = await build_view(tmp_db, question.id, importance_threshold=4)
    view_narrow = await build_view(tmp_db, question.id, importance_threshold=2)

    broad_items = [
        item
        for s in view_broad.sections
        for item in s.items
        if item.page.headline == "Low-priority claim"
    ]
    narrow_items = [
        item
        for s in view_narrow.sections
        for item in s.items
        if item.page.headline == "Low-priority claim"
    ]
    assert len(broad_items) > 0
    assert len(narrow_items) == 0


async def test_superseded_pages_excluded(tmp_db, question):
    claim = await _make_claim(
        tmp_db, question, "Old superseded claim",
        credence=8, robustness=4, importance=0,
    )
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
    child1 = await _make_child_question(tmp_db, question, "Level 1 child")
    await _make_child_question(tmp_db, child1, "Level 2 child")

    view = await build_view(tmp_db, question.id)

    assert view.health.max_depth == 2


async def test_sections_ordered_by_definition(tmp_db, question):
    await _make_claim(
        tmp_db, question, "Core",
        credence=8, robustness=4, importance=0,
    )
    await _make_child_question(tmp_db, question, "Uncertain child")
    await _make_judgement(tmp_db, question, "Current judgement")

    view = await build_view(tmp_db, question.id)

    section_names = [s.name for s in view.sections]
    expected_order = [
        s for s, _ in [
            ("current_position", ""),
            ("core_findings", ""),
            ("key_uncertainties", ""),
        ]
        if s in section_names
    ]
    assert section_names == expected_order


async def test_render_includes_section_headers(tmp_db, question):
    await _make_claim(
        tmp_db, question, "Finding A",
        credence=8, robustness=4, importance=0,
    )
    await _make_child_question(tmp_db, question, "Open question B")

    view = await build_view(tmp_db, question.id)
    rendered = render_view_as_context(view)

    assert "## Core Findings" in rendered
    assert "## Key Uncertainties" in rendered


async def test_render_includes_health_stats(tmp_db, question):
    await _make_claim(
        tmp_db, question, "Scored claim",
        credence=7, robustness=3, importance=1,
    )

    view = await build_view(tmp_db, question.id)
    rendered = render_view_as_context(view)

    assert "Research health" in rendered
    assert "1 pages" in rendered
