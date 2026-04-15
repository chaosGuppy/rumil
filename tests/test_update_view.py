"""Tests for UpdateViewCall: incremental View updates."""

import pytest
import pytest_asyncio

from rumil.calls.update_view import (
    DeepReviewBatchResponse,
    DemotionChoice,
    ItemReview,
    ProposedItem,
    PruneDecision,
    TriageFlag,
    UnscoredItemScore,
    UpdateViewCall,
    UpdateViewContext,
    UpdateViewWorkspaceUpdater,
    _parse_prompt_sections,
    _render_item_compact,
    _render_item_full,
)
from rumil.calls.stages import CallInfra
from rumil.constants import DEFAULT_VIEW_SECTIONS
from rumil.models import (
    Call,
    CallStatus,
    CallType,
    LinkType,
    Page,
    PageLayer,
    PageLink,
    PageType,
    Workspace,
)
from rumil.moves.base import MoveState
from rumil.tracing.tracer import CallTrace


def _page(page_type: PageType, headline: str, **overrides) -> Page:
    defaults = {
        "page_type": page_type,
        "layer": PageLayer.SQUIDGY,
        "workspace": Workspace.RESEARCH,
        "content": f"Content for {headline}",
        "headline": headline,
        "abstract": f"Abstract of {headline}",
    }
    defaults.update(overrides)
    return Page(**defaults)


def _question(headline: str = "Test question", **kw) -> Page:
    return _page(PageType.QUESTION, headline, **kw)


def _view(headline: str = "View: Test question", **kw) -> Page:
    return _page(
        PageType.VIEW,
        headline,
        layer=PageLayer.WIKI,
        sections=list(DEFAULT_VIEW_SECTIONS),
        **kw,
    )


def _view_item(headline: str = "Test item", **kw) -> Page:
    defaults = {"credence": 6, "robustness": 3}
    defaults.update(kw)
    return _page(PageType.VIEW_ITEM, headline, layer=PageLayer.WIKI, **defaults)


def _claim(headline: str = "Test claim", **kw) -> Page:
    return _page(PageType.CLAIM, headline, credence=6, robustness=3, **kw)


def _view_item_link(view_id: str, item_id: str, **kw) -> PageLink:
    defaults = {
        "from_page_id": view_id,
        "to_page_id": item_id,
        "link_type": LinkType.VIEW_ITEM,
        "importance": 3,
        "section": "key_evidence",
        "position": 0,
    }
    defaults.update(kw)
    return PageLink(**defaults)


@pytest_asyncio.fixture
async def view_setup(tmp_db):
    """Create a question with a View and 3 VIEW_ITEM pages linked to it."""
    q = _question()
    v = _view()
    items = [
        _view_item(f"Item {i}", credence=5 + i, robustness=2)
        for i in range(3)
    ]

    await tmp_db.save_page(q)
    await tmp_db.save_page(v)
    for item in items:
        await tmp_db.save_page(item)

    await tmp_db.save_link(PageLink(
        from_page_id=v.id,
        to_page_id=q.id,
        link_type=LinkType.VIEW_OF,
    ))

    for i, item in enumerate(items):
        await tmp_db.save_link(_view_item_link(
            v.id,
            item.id,
            importance=3,
            section="key_evidence",
            position=i,
        ))

    return q, v, items


@pytest_asyncio.fixture
async def call_infra(tmp_db, view_setup):
    """Build a CallInfra for UPDATE_VIEW tests."""
    q, v, items = view_setup
    call = Call(
        call_type=CallType.UPDATE_VIEW,
        workspace=Workspace.RESEARCH,
        scope_page_id=q.id,
        status=CallStatus.PENDING,
    )
    await tmp_db.save_call(call)
    return CallInfra(
        question_id=q.id,
        call=call,
        db=tmp_db,
        trace=CallTrace(call.id, tmp_db),
        state=MoveState(call, tmp_db),
    )


def test_parse_prompt_sections_extracts_markers():
    text = (
        "intro text\n"
        "<!-- PHASE:alpha -->\nalpha content\n"
        "<!-- PHASE:beta -->\nbeta content\n"
    )
    sections = _parse_prompt_sections(text)
    assert "alpha" in sections
    assert "beta" in sections
    assert "alpha content" in sections["alpha"]
    assert "beta content" in sections["beta"]


def test_parse_prompt_sections_handles_extra_attrs():
    text = (
        "<!-- PHASE:ctx — DO NOT RENAME THIS MARKER -->\n"
        "Some context here"
    )
    sections = _parse_prompt_sections(text)
    assert "ctx" in sections
    assert "Some context here" in sections["ctx"]


def test_parse_prompt_sections_empty():
    assert _parse_prompt_sections("no markers here") == {}


def test_render_item_compact_shows_scores():
    page = _view_item("Important finding", credence=7, robustness=4)
    link = _view_item_link("view-id", page.id, importance=5, section="key_evidence")
    rendered = _render_item_compact(page, link)
    assert "I5" in rendered
    assert "C7/R4" in rendered
    assert "Important finding" in rendered
    assert page.id[:8] in rendered


def test_render_item_compact_with_null_importance():
    page = _view_item("Unscored")
    link = _view_item_link("view-id", page.id, importance=None)
    rendered = _render_item_compact(page, link)
    assert "I?" in rendered


def test_render_item_full_includes_cited_pages():
    page = _view_item("Main item")
    link = _view_item_link("view-id", page.id)

    cited_claim = _claim("Supporting evidence")
    cite_link = PageLink(
        from_page_id=page.id,
        to_page_id=cited_claim.id,
        link_type=LinkType.DEPENDS_ON,
    )

    rendered = _render_item_full(
        page,
        link,
        cited_pages={cited_claim.id: cited_claim},
        item_links=[cite_link],
    )
    assert "Supporting evidence" in rendered
    assert "Cited evidence" in rendered
    assert cited_claim.id[:8] in rendered


def test_render_item_full_without_citations():
    page = _view_item("Solo item")
    link = _view_item_link("view-id", page.id)
    rendered = _render_item_full(page, link)
    assert "Solo item" in rendered
    assert "Cited evidence" not in rendered


async def test_create_new_view_and_copy_items(tmp_db, view_setup):
    """UpdateViewCall should create a new View, supersede the old one,
    and copy all VIEW_ITEM links."""
    q, old_view, items = view_setup

    call = Call(
        call_type=CallType.UPDATE_VIEW,
        workspace=Workspace.RESEARCH,
        scope_page_id=q.id,
        status=CallStatus.PENDING,
    )
    await tmp_db.save_call(call)

    runner = UpdateViewCall(q.id, call, tmp_db)

    old_id, new_id = await runner._create_new_view_and_copy_items()

    assert old_id == old_view.id
    assert new_id != old_view.id

    old_page = await tmp_db.get_page(old_view.id)
    assert old_page is not None
    assert old_page.is_superseded

    new_page = await tmp_db.get_page(new_id)
    assert new_page is not None
    assert new_page.page_type == PageType.VIEW
    assert not new_page.is_superseded

    new_items = await tmp_db.get_view_items(new_id)
    new_item_ids = {page.id for page, _ in new_items}
    for item in items:
        assert item.id in new_item_ids, f"Item {item.headline} should be copied"

    for _, link in new_items:
        assert link.importance == 3
        assert link.section == "key_evidence"

    found_view = await tmp_db.get_view_for_question(q.id)
    assert found_view is not None
    assert found_view.id == new_id


async def test_create_new_view_preserves_link_metadata(tmp_db):
    """Link metadata (importance, section, position) should survive the copy."""
    q = _question()
    v = _view()
    item = _view_item("Special item")
    await tmp_db.save_page(q)
    await tmp_db.save_page(v)
    await tmp_db.save_page(item)
    await tmp_db.save_link(PageLink(
        from_page_id=v.id,
        to_page_id=q.id,
        link_type=LinkType.VIEW_OF,
    ))
    await tmp_db.save_link(_view_item_link(
        v.id, item.id, importance=5, section="confident_views", position=7,
    ))

    call = Call(
        call_type=CallType.UPDATE_VIEW,
        workspace=Workspace.RESEARCH,
        scope_page_id=q.id,
        status=CallStatus.PENDING,
    )
    await tmp_db.save_call(call)
    runner = UpdateViewCall(q.id, call, tmp_db)

    _, new_id = await runner._create_new_view_and_copy_items()
    new_items = await tmp_db.get_view_items(new_id)
    assert len(new_items) == 1
    _, link = new_items[0]
    assert link.importance == 5
    assert link.section == "confident_views"
    assert link.position == 7


async def test_create_new_view_raises_without_existing_view(tmp_db):
    """UpdateViewCall should raise if there's no existing View to update."""
    q = _question()
    await tmp_db.save_page(q)
    call = Call(
        call_type=CallType.UPDATE_VIEW,
        workspace=Workspace.RESEARCH,
        scope_page_id=q.id,
        status=CallStatus.PENDING,
    )
    await tmp_db.save_call(call)
    runner = UpdateViewCall(q.id, call, tmp_db)
    with pytest.raises(RuntimeError, match="requires an existing View"):
        await runner._create_new_view_and_copy_items()


async def test_apply_item_score_updates_link(tmp_db, view_setup, call_infra):
    """_apply_item_score should update the VIEW_ITEM link's importance and section."""
    _, v, items = view_setup
    updater = UpdateViewWorkspaceUpdater(v.id, CallType.UPDATE_VIEW)

    target = items[0]
    view_items = await tmp_db.get_view_items(v.id)
    target_page, target_link = next(
        (p, l) for p, l in view_items if p.id == target.id
    )
    score = UnscoredItemScore(
        item_id=target.id[:8],
        importance=5,
        section="confident_views",
    )

    changed = await updater._apply_item_score(tmp_db, score, target_page, target_link)
    assert changed

    result_items = await tmp_db.get_view_items(v.id)
    for page, link in result_items:
        if page.id == target.id:
            assert link.importance == 5
            assert link.section == "confident_views"
            break
    else:
        pytest.fail("Target item not found in view items")


async def test_apply_item_score_updates_credence_robustness(
    tmp_db, view_setup, call_infra,
):
    """_apply_item_score with credence/robustness overrides should update the page."""
    _, v, items = view_setup
    updater = UpdateViewWorkspaceUpdater(v.id, CallType.UPDATE_VIEW)

    target = items[1]
    view_items = await tmp_db.get_view_items(v.id)
    target_page, target_link = next(
        (p, l) for p, l in view_items if p.id == target.id
    )
    score = UnscoredItemScore(
        item_id=target.id[:8],
        importance=4,
        section="live_hypotheses",
        credence=8,
        robustness=4,
    )

    await updater._apply_item_score(tmp_db, score, target_page, target_link)

    updated_page = await tmp_db.get_page(target.id)
    assert updated_page is not None
    assert updated_page.credence == 8
    assert updated_page.robustness == 4


async def test_apply_demotion_lowers_importance(tmp_db, view_setup, call_infra):
    """_apply_demotion should lower the item's importance on its VIEW_ITEM link."""
    _, v, items = view_setup
    updater = UpdateViewWorkspaceUpdater(v.id, CallType.UPDATE_VIEW)

    target = items[0]
    view_items = await tmp_db.get_view_items(v.id)
    _, target_link = next(
        (p, l) for p, l in view_items if p.id == target.id
    )
    demotion = DemotionChoice(
        item_id=target.id[:8],
        new_importance=1,
        reasoning="Not central",
    )
    await updater._apply_demotion(tmp_db, demotion, target.id, target_link)

    result_items = await tmp_db.get_view_items(v.id)
    for page, link in result_items:
        if page.id == target.id:
            assert link.importance == 1
            break
    else:
        pytest.fail("Demoted item not found")


async def test_unlink_item_removes_link_preserves_page(tmp_db, view_setup, call_infra):
    """_unlink_item should delete the VIEW_ITEM link but leave the page intact."""
    _, v, items = view_setup
    updater = UpdateViewWorkspaceUpdater(v.id, CallType.UPDATE_VIEW)

    target = items[0]
    view_items = await tmp_db.get_view_items(v.id)
    _, target_link = next(
        (p, l) for p, l in view_items if p.id == target.id
    )
    did_unlink = await updater._unlink_item(tmp_db, target.id, target_link)
    assert did_unlink

    result_items = await tmp_db.get_view_items(v.id)
    remaining_ids = {page.id for page, _ in result_items}
    assert target.id not in remaining_ids

    page = await tmp_db.get_page(target.id)
    assert page is not None
    assert not page.is_superseded


async def test_apply_item_review_keep_returns_false(tmp_db, view_setup, call_infra):
    """A 'keep' review should be a no-op."""
    _, v, items = view_setup
    updater = UpdateViewWorkspaceUpdater(v.id, CallType.UPDATE_VIEW)

    target = items[0]
    view_items = await tmp_db.get_view_items(v.id)
    target_page, target_link = next(
        (p, l) for p, l in view_items if p.id == target.id
    )
    review = ItemReview(
        item_id=target.id[:8],
        action="keep",
    )
    changed = await updater._apply_item_review(
        call_infra, review, target.id, target_page, target_link
    )
    assert changed is False


async def test_apply_item_review_adjust(tmp_db, view_setup, call_infra):
    """An 'adjust' review should update link metadata and page fields."""
    _, v, items = view_setup
    updater = UpdateViewWorkspaceUpdater(v.id, CallType.UPDATE_VIEW)

    target = items[1]
    view_items = await tmp_db.get_view_items(v.id)
    target_page, target_link = next(
        (p, l) for p, l in view_items if p.id == target.id
    )
    review = ItemReview(
        item_id=target.id[:8],
        action="adjust",
        new_importance=5,
        new_section="confident_views",
        new_credence=9,
    )
    changed = await updater._apply_item_review(
        call_infra, review, target.id, target_page, target_link
    )
    assert changed

    result_items = await tmp_db.get_view_items(v.id)
    for page, link in result_items:
        if page.id == target.id:
            assert link.importance == 5
            assert link.section == "confident_views"
            break
    else:
        pytest.fail("Adjusted item not found")

    page = await tmp_db.get_page(target.id)
    assert page is not None
    assert page.credence == 9


async def test_apply_item_review_supersede(tmp_db, view_setup, call_infra):
    """A 'supersede' review should create a new page, supersede the old, and relink."""
    _, v, items = view_setup
    updater = UpdateViewWorkspaceUpdater(v.id, CallType.UPDATE_VIEW)

    target = items[2]
    view_items = await tmp_db.get_view_items(v.id)
    target_page, target_link = next(
        (p, l) for p, l in view_items if p.id == target.id
    )
    review = ItemReview(
        item_id=target.id[:8],
        action="supersede",
        new_headline="Revised finding",
        new_content="Updated content after new evidence.",
        new_importance=4,
        new_section="live_hypotheses",
    )
    changed = await updater._apply_item_review(
        call_infra, review, target.id, target_page, target_link
    )
    assert changed

    old_page = await tmp_db.get_page(target.id)
    assert old_page is not None
    assert old_page.is_superseded

    result_items = await tmp_db.get_view_items(v.id)
    remaining_ids = {page.id for page, _ in result_items}
    assert target.id not in remaining_ids

    new_items = [
        (p, l) for p, l in result_items if p.headline == "Revised finding"
    ]
    assert len(new_items) == 1
    new_page, new_link = new_items[0]
    assert new_page.content == "Updated content after new evidence."
    assert new_link.importance == 4
    assert new_link.section == "live_hypotheses"


async def test_create_proposed_item(tmp_db, view_setup, call_infra):
    """_create_proposed_item should add a new VIEW_ITEM to the View."""
    _, v, _ = view_setup
    updater = UpdateViewWorkspaceUpdater(v.id, CallType.UPDATE_VIEW)

    proposal = ProposedItem(
        headline="Newly discovered pattern",
        content="Evidence suggests a recurring pattern in the data.",
        credence=7,
        robustness=3,
        importance=4,
        section="key_evidence",
        reasoning="Fills a gap in evidence coverage",
    )

    new_id = await updater._create_proposed_item(call_infra, proposal)
    assert new_id is not None

    page = await tmp_db.get_page(new_id)
    assert page is not None
    assert page.page_type == PageType.VIEW_ITEM
    assert page.headline == "Newly discovered pattern"
    assert page.credence == 7

    result_items = await tmp_db.get_view_items(v.id)
    new_item_ids = {p.id for p, _ in result_items}
    assert new_id in new_item_ids


async def test_phase_score_unscored_skips_when_all_scored(
    tmp_db, view_setup, call_infra,
):
    """Phase 1 should skip (trace PhaseSkippedEvent) when all items already scored."""
    _, v, _ = view_setup
    updater = UpdateViewWorkspaceUpdater(v.id, CallType.UPDATE_VIEW)

    messages = [
        {"role": "user", "content": "context"},
        {"role": "assistant", "content": "Understood."},
    ]
    sections = {"score_unscored": "Score them.", "context": ""}

    result_messages = await updater._phase_score_unscored(
        call_infra, "system", sections, messages,
    )
    assert len(result_messages) == len(messages)


async def test_phase_triage_skips_when_no_scored_items(tmp_db, call_infra):
    """Triage should skip when there are no scored items at all."""
    q = await tmp_db.get_page(call_infra.question_id)
    v = _view("Empty view")
    await tmp_db.save_page(v)
    await tmp_db.save_link(PageLink(
        from_page_id=v.id,
        to_page_id=q.id,
        link_type=LinkType.VIEW_OF,
    ))

    updater = UpdateViewWorkspaceUpdater(v.id, CallType.UPDATE_VIEW)
    messages = [
        {"role": "user", "content": "context"},
        {"role": "assistant", "content": "Understood."},
    ]

    result_messages, flagged_ids = await updater._phase_triage(
        call_infra, "system", {}, messages,
    )
    assert len(result_messages) == len(messages)
    assert flagged_ids == []


async def test_phase_prune_skips_when_no_low_items(
    tmp_db, view_setup, call_infra,
):
    """Prune phase should skip when there are no I1/I2 items."""
    _, v, _ = view_setup
    updater = UpdateViewWorkspaceUpdater(v.id, CallType.UPDATE_VIEW)

    messages = [
        {"role": "user", "content": "context"},
        {"role": "assistant", "content": "Understood."},
    ]

    result_messages = await updater._phase_prune(
        call_infra, "system", {}, messages,
    )
    assert len(result_messages) == len(messages)


async def test_phase_enforce_caps_skips_within_limits(
    tmp_db, view_setup, call_infra,
):
    """Enforce caps should skip when all levels are within their caps."""
    _, v, _ = view_setup
    updater = UpdateViewWorkspaceUpdater(v.id, CallType.UPDATE_VIEW)

    messages = [
        {"role": "user", "content": "context"},
        {"role": "assistant", "content": "Understood."},
    ]

    result_messages = await updater._phase_enforce_caps(
        call_infra, "system", {}, messages,
    )
    assert len(result_messages) == len(messages)


async def test_view_setup_with_unscored_items(tmp_db):
    """Verify that unscored items (importance=None) are detected by get_view_items."""
    q = _question()
    v = _view()
    scored_item = _view_item("Scored")
    unscored_item = _view_item("Unscored")
    await tmp_db.save_page(q)
    await tmp_db.save_page(v)
    await tmp_db.save_page(scored_item)
    await tmp_db.save_page(unscored_item)

    await tmp_db.save_link(PageLink(
        from_page_id=v.id,
        to_page_id=q.id,
        link_type=LinkType.VIEW_OF,
    ))
    await tmp_db.save_link(_view_item_link(
        v.id, scored_item.id, importance=3,
    ))
    await tmp_db.save_link(_view_item_link(
        v.id, unscored_item.id, importance=None,
    ))

    items = await tmp_db.get_view_items(v.id)
    importances = {page.headline: link.importance for page, link in items}
    assert importances["Scored"] == 3
    assert importances["Unscored"] is None
