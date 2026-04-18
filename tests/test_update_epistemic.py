"""Tests for the update_epistemic move.

Covers: basic score write, cascade emission on material score change,
cascade thresholds from settings, and edge cases (no prior score,
question-page rejection).
"""

import pytest

from rumil.cascades import CASCADE_FIELDS
from rumil.database import DB
from rumil.models import (
    Call,
    CallStatus,
    CallType,
    LinkType,
    Page,
    PageLayer,
    PageLink,
    PageType,
    SuggestionType,
    Workspace,
)
from rumil.moves.update_epistemic import UpdateEpistemicPayload, execute
from rumil.settings import override_settings


def _claim(headline: str, credence: int = 5, robustness: int = 3) -> Page:
    return Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=f"content of {headline}",
        headline=headline,
        credence=credence,
        robustness=robustness,
    )


def _question(headline: str = "Q?") -> Page:
    return Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=headline,
        headline=headline,
    )


async def _make_call(tmp_db: DB, scope_page: Page) -> Call:
    call = Call(
        call_type=CallType.ASSESS,
        workspace=Workspace.RESEARCH,
        scope_page_id=scope_page.id,
        status=CallStatus.PENDING,
    )
    await tmp_db.save_call(call)
    return call


async def test_update_epistemic_persists_new_scores(tmp_db: DB):
    page = _claim("target", credence=5, robustness=3)
    await tmp_db.save_page(page)
    call = await _make_call(tmp_db, page)

    payload = UpdateEpistemicPayload(
        page_id=page.id,
        credence=8,
        robustness=4,
        reasoning="fresh evidence",
    )
    result = await execute(payload, call, tmp_db)

    assert "C8/R4" in result.message
    score_row, _ = await tmp_db.get_epistemic_score_source(page.id)
    assert score_row is not None
    assert score_row["credence"] == 8
    assert score_row["robustness"] == 4


async def test_update_epistemic_rejects_question_page(tmp_db: DB):
    q = _question("what?")
    await tmp_db.save_page(q)
    call = await _make_call(tmp_db, q)

    payload = UpdateEpistemicPayload(
        page_id=q.id,
        credence=5,
        robustness=3,
        reasoning="shouldn't be allowed",
    )
    result = await execute(payload, call, tmp_db)

    assert "question" in result.message.lower()
    score_row, _ = await tmp_db.get_epistemic_score_source(q.id)
    assert score_row is None


async def test_update_epistemic_emits_cascade_on_material_change(tmp_db: DB):
    upstream = _claim("upstream", credence=8, robustness=4)
    dependent = _claim("dependent", credence=6)
    await tmp_db.save_page(upstream)
    await tmp_db.save_page(dependent)
    await tmp_db.save_link(
        PageLink(
            from_page_id=dependent.id,
            to_page_id=upstream.id,
            link_type=LinkType.DEPENDS_ON,
            strength=4.0,
            reasoning="uses upstream",
        )
    )
    call = await _make_call(tmp_db, upstream)

    payload = UpdateEpistemicPayload(
        page_id=upstream.id,
        credence=4,
        robustness=4,
        reasoning="evidence weakened",
    )
    result = await execute(payload, call, tmp_db)

    assert "cascade" in result.message.lower()
    pending = await tmp_db.get_pending_suggestions()
    cascades = [s for s in pending if s.suggestion_type == SuggestionType.CASCADE_REVIEW]
    assert len(cascades) == 1
    assert cascades[0].target_page_id == dependent.id
    assert cascades[0].source_page_id == upstream.id


async def test_update_epistemic_no_cascade_below_threshold(tmp_db: DB):
    upstream = _claim("upstream", credence=5, robustness=3)
    dependent = _claim("dependent")
    await tmp_db.save_page(upstream)
    await tmp_db.save_page(dependent)
    await tmp_db.save_link(
        PageLink(
            from_page_id=dependent.id,
            to_page_id=upstream.id,
            link_type=LinkType.DEPENDS_ON,
            strength=4.0,
            reasoning="uses upstream",
        )
    )
    call = await _make_call(tmp_db, upstream)

    payload = UpdateEpistemicPayload(
        page_id=upstream.id,
        credence=6,
        robustness=3,
        reasoning="tiny refinement",
    )
    await execute(payload, call, tmp_db)

    pending = await tmp_db.get_pending_suggestions()
    cascades = [s for s in pending if s.suggestion_type == SuggestionType.CASCADE_REVIEW]
    assert cascades == []


async def test_update_epistemic_cascade_on_robustness_change_uses_settings(tmp_db: DB):
    """Delta-1 robustness change should fire a cascade under default
    settings (cascade_robustness_delta_threshold=1).
    """
    upstream = _claim("upstream", credence=5, robustness=4)
    dependent = _claim("dependent")
    await tmp_db.save_page(upstream)
    await tmp_db.save_page(dependent)
    await tmp_db.save_link(
        PageLink(
            from_page_id=dependent.id,
            to_page_id=upstream.id,
            link_type=LinkType.DEPENDS_ON,
            strength=4.0,
            reasoning="uses upstream",
        )
    )
    call = await _make_call(tmp_db, upstream)

    payload = UpdateEpistemicPayload(
        page_id=upstream.id,
        credence=5,
        robustness=3,
        reasoning="slightly less robust",
    )
    await execute(payload, call, tmp_db)

    pending = await tmp_db.get_pending_suggestions()
    cascades = [s for s in pending if s.suggestion_type == SuggestionType.CASCADE_REVIEW]
    assert len(cascades) == 1


async def test_update_epistemic_threshold_respects_override(tmp_db: DB):
    """A tuned-up credence threshold should suppress cascades that the
    default would have fired.
    """
    upstream = _claim("upstream", credence=5, robustness=3)
    dependent = _claim("dependent")
    await tmp_db.save_page(upstream)
    await tmp_db.save_page(dependent)
    await tmp_db.save_link(
        PageLink(
            from_page_id=dependent.id,
            to_page_id=upstream.id,
            link_type=LinkType.DEPENDS_ON,
            strength=4.0,
            reasoning="uses upstream",
        )
    )
    call = await _make_call(tmp_db, upstream)

    with override_settings(
        cascade_credence_delta_threshold=5,
        cascade_robustness_delta_threshold=5,
        rumil_test_mode="1",
    ):
        payload = UpdateEpistemicPayload(
            page_id=upstream.id,
            credence=8,
            robustness=3,
            reasoning="would normally cascade",
        )
        await execute(payload, call, tmp_db)

    pending = await tmp_db.get_pending_suggestions()
    cascades = [s for s in pending if s.suggestion_type == SuggestionType.CASCADE_REVIEW]
    assert cascades == []


async def test_update_epistemic_ignores_missing_page(tmp_db: DB):
    """Calling update_epistemic on a nonexistent page should fail
    gracefully without writing anything.
    """
    q = _question()
    await tmp_db.save_page(q)
    call = await _make_call(tmp_db, q)

    payload = UpdateEpistemicPayload(
        page_id="11111111-1111-1111-1111-111111111111",
        credence=5,
        robustness=3,
        reasoning="ghost page",
    )
    result = await execute(payload, call, tmp_db)

    assert "could not resolve" in result.message.lower() or "not found" in result.message.lower()


def test_cascade_fields_includes_all_three_score_dimensions():
    """Regression guard: cascades must consider credence AND robustness
    AND importance — not accidentally dropped to one dimension.
    """
    assert {"credence", "robustness", "importance"} == CASCADE_FIELDS


@pytest.mark.parametrize(
    ("old_credence", "new_credence", "expect_cascade"),
    [
        (5, 5, False),
        (5, 6, False),
        (5, 7, True),
        (8, 6, True),
        (8, 5, True),
    ],
)
async def test_update_epistemic_credence_threshold_boundary(
    tmp_db: DB,
    old_credence: int,
    new_credence: int,
    expect_cascade: bool,
):
    upstream = _claim("upstream", credence=old_credence, robustness=3)
    dependent = _claim("dependent")
    await tmp_db.save_page(upstream)
    await tmp_db.save_page(dependent)
    await tmp_db.save_link(
        PageLink(
            from_page_id=dependent.id,
            to_page_id=upstream.id,
            link_type=LinkType.DEPENDS_ON,
            strength=4.0,
            reasoning="uses upstream",
        )
    )
    call = await _make_call(tmp_db, upstream)

    payload = UpdateEpistemicPayload(
        page_id=upstream.id,
        credence=new_credence,
        robustness=3,
        reasoning="boundary test",
    )
    await execute(payload, call, tmp_db)

    pending = await tmp_db.get_pending_suggestions()
    cascades = [s for s in pending if s.suggestion_type == SuggestionType.CASCADE_REVIEW]
    assert bool(cascades) is expect_cascade
