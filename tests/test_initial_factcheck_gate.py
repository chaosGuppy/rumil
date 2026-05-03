"""Tests for the empty-workspace gate that drops ``SCOUT_FACTCHECKS`` from
the initial prioritization fan-out when there are no claims to verify yet.

Covers both the DB-side ``has_any_active_claim`` predicate and the
orchestrator wiring that consumes it.
"""

import pytest

from rumil.calls.common import RunCallResult
from rumil.models import (
    CallType,
    Page,
    PageLayer,
    PageType,
    Workspace,
)
from rumil.orchestrators.two_phase import TwoPhaseOrchestrator


async def _save_claim(db, headline: str, *, superseded: bool = False) -> Page:
    page = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=headline,
        headline=headline,
        is_superseded=superseded,
    )
    await db.save_page(page)
    return page


@pytest.mark.asyncio
async def test_has_any_active_claim_false_on_empty_project(tmp_db):
    assert await tmp_db.has_any_active_claim() is False


@pytest.mark.asyncio
async def test_has_any_active_claim_true_when_one_active_claim_exists(tmp_db):
    await _save_claim(tmp_db, "some claim")
    assert await tmp_db.has_any_active_claim() is True


@pytest.mark.asyncio
async def test_has_any_active_claim_excludes_superseded_claims(tmp_db):
    await _save_claim(tmp_db, "old claim", superseded=True)
    assert await tmp_db.has_any_active_claim() is False


@pytest.mark.asyncio
async def test_initial_fanout_drops_factchecks_when_workspace_empty(
    tmp_db,
    question_page,
    prio_harness,
):
    """Empty workspace: the LLM should not see scout_factchecks as an option,
    because there are no claims for it to verify (its own prompt redirects
    to scout_web_questions in that case)."""
    prio_harness.prio_queue = [
        RunCallResult(dispatches=[]),
        RunCallResult(dispatches=[]),
    ]

    orch = TwoPhaseOrchestrator(tmp_db, budget_cap=10)
    await orch.run(question_page.id)

    assert prio_harness.prio_calls, "expected at least one prioritization invocation"
    initial_call = prio_harness.prio_calls[0]
    assert initial_call.get("prompt_name") == "two_phase_initial_prioritization"
    assert CallType.SCOUT_FACTCHECKS not in initial_call["dispatch_types"]


@pytest.mark.asyncio
async def test_initial_fanout_keeps_factchecks_when_a_claim_exists(
    tmp_db,
    question_page,
    prio_harness,
):
    """As soon as the workspace has any active claim, scout_factchecks is back
    on the menu — even if the claim is unrelated to the scope question."""
    await _save_claim(tmp_db, "an unrelated existing claim")

    prio_harness.prio_queue = [
        RunCallResult(dispatches=[]),
        RunCallResult(dispatches=[]),
    ]

    orch = TwoPhaseOrchestrator(tmp_db, budget_cap=10)
    await orch.run(question_page.id)

    initial_call = prio_harness.prio_calls[0]
    assert initial_call.get("prompt_name") == "two_phase_initial_prioritization"
    assert CallType.SCOUT_FACTCHECKS in initial_call["dispatch_types"]
