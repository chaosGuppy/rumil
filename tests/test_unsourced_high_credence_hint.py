"""Tests for the "many unverified high-credence claims" hint that
``TwoPhaseOrchestrator._main_phase_prioritization`` injects into the
prioritizer's task prompt.

Two layers:
- ``DB.count_unsourced_high_credence_claims`` — pure DB-level counting,
  filtering by credence threshold and absence of CITES links.
- The orchestrator wiring — when the count clears the module-level
  threshold, the LLM task prompt actually contains the Note.
"""

import pytest

from rumil.calls.common import RunCallResult
from rumil.models import (
    LinkType,
    Page,
    PageLayer,
    PageLink,
    PageType,
    Workspace,
)
from rumil.orchestrators.two_phase import (
    UNSOURCED_HIGH_CREDENCE_THRESHOLD,
    TwoPhaseOrchestrator,
)


async def _make_claim(db, *, credence: int | None, headline: str) -> Page:
    page = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=headline,
        headline=headline,
        credence=credence,
        credence_reasoning="" if credence is None else "test",
    )
    await db.save_page(page)
    return page


async def _make_source(db, headline: str) -> Page:
    page = Page(
        page_type=PageType.SOURCE,
        layer=PageLayer.WIKI,
        workspace=Workspace.RESEARCH,
        content=headline,
        headline=headline,
    )
    await db.save_page(page)
    return page


@pytest.mark.asyncio
async def test_count_excludes_low_credence_claims(tmp_db):
    await _make_claim(tmp_db, credence=5, headline="below threshold")
    await _make_claim(tmp_db, credence=None, headline="no credence")

    assert await tmp_db.count_unsourced_high_credence_claims() == 0


@pytest.mark.asyncio
async def test_count_includes_high_credence_unsourced_claims(tmp_db):
    await _make_claim(tmp_db, credence=6, headline="just over threshold")
    await _make_claim(tmp_db, credence=9, headline="very confident")

    assert await tmp_db.count_unsourced_high_credence_claims() == 2


@pytest.mark.asyncio
async def test_count_excludes_claims_that_cite_a_source(tmp_db):
    cited = await _make_claim(tmp_db, credence=8, headline="grounded claim")
    uncited = await _make_claim(tmp_db, credence=8, headline="ungrounded claim")
    source = await _make_source(tmp_db, headline="some paper")
    await tmp_db.save_link(
        PageLink(
            from_page_id=cited.id,
            to_page_id=source.id,
            link_type=LinkType.CITES,
        )
    )

    assert await tmp_db.count_unsourced_high_credence_claims() == 1
    # Sanity: ID list shape — the uncited one is what's surfaced.
    assert uncited.id != cited.id


@pytest.mark.asyncio
async def test_count_threshold_is_inclusive(tmp_db):
    """credence == threshold counts as 'high'; credence == threshold-1 does not."""
    await _make_claim(tmp_db, credence=6, headline="exactly at threshold")
    assert await tmp_db.count_unsourced_high_credence_claims(credence_threshold=6) == 1
    assert await tmp_db.count_unsourced_high_credence_claims(credence_threshold=7) == 0


@pytest.mark.asyncio
async def test_main_phase_prompt_includes_note_when_threshold_exceeded(
    tmp_db,
    question_page,
    prio_harness,
):
    """When enough unsourced high-credence claims exist, the
    main-phase prioritization task prompt carries the Note."""
    for i in range(UNSOURCED_HIGH_CREDENCE_THRESHOLD):
        await _make_claim(tmp_db, credence=8, headline=f"unsourced claim {i}")

    prio_harness.prio_queue = [
        RunCallResult(dispatches=[]),
        RunCallResult(dispatches=[]),
    ]

    orch = TwoPhaseOrchestrator(tmp_db, budget_cap=10)
    orch._executed_since_last_plan = True
    orch._invocation = 1
    await orch._main_phase_prioritization(
        question_page.id,
        budget=10,
        parent_call_id=None,
    )

    assert prio_harness.prio_calls, "expected at least one prioritization invocation"
    task = prio_harness.prio_calls[-1]["task"]
    assert "credence ≥6" in task
    assert str(UNSOURCED_HIGH_CREDENCE_THRESHOLD) in task
    assert "dispatch_web_factcheck" in task


@pytest.mark.asyncio
async def test_main_phase_prompt_omits_note_when_below_threshold(
    tmp_db,
    question_page,
    prio_harness,
):
    """Below the threshold, the Note must not appear — keeps the prompt
    quiet on small workspaces and avoids habituating the model to it."""
    for i in range(UNSOURCED_HIGH_CREDENCE_THRESHOLD - 1):
        await _make_claim(tmp_db, credence=8, headline=f"unsourced claim {i}")

    prio_harness.prio_queue = [
        RunCallResult(dispatches=[]),
        RunCallResult(dispatches=[]),
    ]

    orch = TwoPhaseOrchestrator(tmp_db, budget_cap=10)
    orch._executed_since_last_plan = True
    orch._invocation = 1
    await orch._main_phase_prioritization(
        question_page.id,
        budget=10,
        parent_call_id=None,
    )

    task = prio_harness.prio_calls[-1]["task"]
    assert "credence ≥6" not in task
    assert "unverified retrievals" not in task
