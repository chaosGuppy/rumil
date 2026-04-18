"""Tests for CascadeOrchestrator + CascadeReviewPolicy.

Mocks assess_question entirely — no real LLM calls. The behavioural
contract we assert is observable state: were the expected cascades
processed, in what order, and what ended up in the suggestions table?
"""

from unittest.mock import AsyncMock

import pytest

from rumil.cascades import check_cascades
from rumil.database import DB
from rumil.models import (
    LinkType,
    Page,
    PageLayer,
    PageLink,
    PageType,
    SuggestionStatus,
    SuggestionType,
    Workspace,
)
from rumil.orchestrators.cascade import (
    CascadeOrchestrator,
    pending_cascade_suggestions,
)
from rumil.orchestrators.policy_layer import (
    CascadeReviewPolicy,
    DispatchCall,
    QuestionState,
)


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


async def _link_depends_on(db: DB, dependent: Page, upstream: Page) -> None:
    await db.save_link(
        PageLink(
            from_page_id=dependent.id,
            to_page_id=upstream.id,
            link_type=LinkType.DEPENDS_ON,
            strength=4.0,
            reasoning="builds on upstream",
        )
    )


async def _setup_chain(tmp_db: DB) -> tuple[Page, Page, Page]:
    """Build A depends on B depends on C. Return (a, b, c)."""
    a = _claim("A", credence=7)
    b = _claim("B", credence=6)
    c = _claim("C", credence=8)
    await tmp_db.save_page(a)
    await tmp_db.save_page(b)
    await tmp_db.save_page(c)
    await _link_depends_on(tmp_db, a, b)
    await _link_depends_on(tmp_db, b, c)
    return a, b, c


@pytest.fixture
def patched_assess(mocker):
    """Patch assess_question at the cascade module boundary.

    Returns a stub that records which targets got assessed, consumes one
    unit of budget per call (matching real assess_question behaviour),
    and returns a synthetic call-id so the orchestrator treats the call
    as successful.
    """
    assessed: list[str] = []

    async def _assess(question_id, db, **kwargs):
        ok = await db.consume_budget(1)
        if not ok:
            return None
        assessed.append(question_id)
        return f"call-{len(assessed)}"

    mocker.patch(
        "rumil.orchestrators.cascade.assess_question",
        side_effect=_assess,
    )
    return assessed


async def test_cascade_fires_on_dependent_claim(tmp_db: DB, patched_assess):
    """C's credence drops materially -> cascade flagged against B
    (which depends on C). Running the orchestrator then assesses B.
    """
    a, b, c = await _setup_chain(tmp_db)

    suggestions = await check_cascades(
        tmp_db,
        c.id,
        {"credence": (8, 5)},
    )
    assert len(suggestions) == 1
    assert suggestions[0].target_page_id == b.id

    orch = CascadeOrchestrator(tmp_db)
    await orch.run(a.id)

    assert b.id in patched_assess
    all_suggestions = await tmp_db.get_suggestions(status="accepted")
    assert len(all_suggestions) == 1
    assert all_suggestions[0].target_page_id == b.id


async def test_cascade_chain_propagates_two_levels(tmp_db: DB, patched_assess, mocker):
    """Full A->B->C chain: after C changes, cascade hits B; after B's
    assess "drops B's credence", a fresh cascade should fire against A.
    Then a second orchestrator run should process that new cascade.
    """
    a, b, c = await _setup_chain(tmp_db)

    await check_cascades(tmp_db, c.id, {"credence": (8, 5)})

    async def _assess_then_cascade_b(question_id, db, **kwargs):
        ok = await db.consume_budget(1)
        if not ok:
            return None
        if question_id == b.id:
            await check_cascades(db, b.id, {"credence": (6, 3)})
        return f"call-for-{question_id[:4]}"

    mocker.patch(
        "rumil.orchestrators.cascade.assess_question",
        side_effect=_assess_then_cascade_b,
    )

    orch = CascadeOrchestrator(tmp_db)
    await orch.run(a.id)

    accepted = await tmp_db.get_suggestions(status="accepted")
    accepted_targets = {s.target_page_id for s in accepted}
    assert b.id in accepted_targets
    assert a.id in accepted_targets


async def test_budget_respected_limits_cascade_runs(tmp_db: DB, patched_assess):
    """With budget=1, only one cascade review should run even when
    multiple pending cascade suggestions exist.
    """
    upstream = _claim("upstream", credence=8)
    dep1 = _claim("dep1")
    dep2 = _claim("dep2")
    for p in (upstream, dep1, dep2):
        await tmp_db.save_page(p)
    for dep in (dep1, dep2):
        await _link_depends_on(tmp_db, dep, upstream)

    await check_cascades(tmp_db, upstream.id, {"credence": (8, 4)})

    pending = pending_cascade_suggestions(await tmp_db.get_pending_suggestions())
    assert len(pending) == 2

    _, used = await tmp_db.get_budget()
    await tmp_db.consume_budget(100 - used - 1)
    assert await tmp_db.budget_remaining() == 1

    orch = CascadeOrchestrator(tmp_db)
    await orch.run(upstream.id)

    assert len(patched_assess) == 1
    remaining_pending = pending_cascade_suggestions(await tmp_db.get_pending_suggestions())
    assert len(remaining_pending) == 1


async def test_stale_suggestion_is_skipped(tmp_db: DB, patched_assess, mocker):
    """A suggestion whose target has been reassessed since creation is
    stale. The orchestrator should dismiss it and not run assess.
    """
    upstream = _claim("upstream", credence=8)
    dep = _claim("dep")
    await tmp_db.save_page(upstream)
    await tmp_db.save_page(dep)
    await _link_depends_on(tmp_db, dep, upstream)

    await check_cascades(tmp_db, upstream.id, {"credence": (8, 4)})
    pending = await tmp_db.get_pending_suggestions()
    assert len(pending) == 1
    suggestion = pending[0]

    from datetime import UTC, datetime, timedelta

    later = datetime.now(UTC) + timedelta(minutes=5)

    async def _fake_score_source(page_id):
        if page_id == dep.id:
            return (
                {
                    "credence": 5,
                    "robustness": 3,
                    "call_id": "synthetic",
                    "created_at": later.isoformat(),
                    "reasoning": "recent reassess",
                },
                None,
            )
        return (None, None)

    mocker.patch.object(
        tmp_db,
        "get_epistemic_score_source",
        side_effect=_fake_score_source,
    )

    orch = CascadeOrchestrator(tmp_db)
    await orch.run(upstream.id)

    assert patched_assess == []

    dismissed = await tmp_db.get_suggestions(status="dismissed")
    assert len(dismissed) == 1
    assert dismissed[0].id == suggestion.id


async def test_no_pending_cascades_noop(tmp_db: DB, patched_assess):
    """With no pending cascade suggestions, orchestrator terminates
    cleanly and runs no assesses.
    """
    a = _claim("standalone")
    await tmp_db.save_page(a)

    orch = CascadeOrchestrator(tmp_db)
    await orch.run(a.id)

    assert patched_assess == []


async def test_cascade_review_policy_returns_dispatch_intent():
    """Policy composition: CascadeReviewPolicy returns a DispatchCall(ASSESS)
    for the newest pending cascade suggestion's target.
    """
    from rumil.models import Suggestion

    class _FakeDB:
        def __init__(self, suggestions):
            self._suggestions = suggestions

        async def get_pending_suggestions(self):
            return list(self._suggestions)

    older = Suggestion(
        suggestion_type=SuggestionType.CASCADE_REVIEW,
        target_page_id="target-older",
        source_page_id="src",
    )
    from datetime import UTC, datetime

    older.created_at = datetime.now(UTC).replace(microsecond=0)

    newer = Suggestion(
        suggestion_type=SuggestionType.CASCADE_REVIEW,
        target_page_id="target-newer",
        source_page_id="src",
    )
    newer.created_at = older.created_at.replace(microsecond=500000)

    fake_db = _FakeDB([older, newer])
    policy = CascadeReviewPolicy(db=fake_db)  # type: ignore[arg-type]

    state = QuestionState(
        question_id="q",
        budget_remaining=10,
        iteration=0,
        consideration_count=0,
        child_question_count=0,
        source_count=0,
        view=None,
        missing_credence_page_ids=[],
        missing_importance_item_ids=[],
        unjudged_child_question_ids=[],
        recent_call_types=[],
    )

    intent = await policy.decide(state)

    assert isinstance(intent, DispatchCall)
    assert intent.call_type.value == "assess"
    assert intent.kwargs["question_id"] == "target-newer"


async def test_cascade_review_policy_returns_none_when_no_cascades():
    """When no CASCADE_REVIEW suggestions exist, policy returns None so
    downstream policies get a turn.
    """

    class _FakeDB:
        async def get_pending_suggestions(self):
            return []

    policy = CascadeReviewPolicy(db=_FakeDB())  # type: ignore[arg-type]
    state = QuestionState(
        question_id="q",
        budget_remaining=10,
        iteration=0,
        consideration_count=0,
        child_question_count=0,
        source_count=0,
        view=None,
        missing_credence_page_ids=[],
        missing_importance_item_ids=[],
        unjudged_child_question_ids=[],
        recent_call_types=[],
    )
    assert await policy.decide(state) is None


async def test_cascade_review_policy_filters_non_cascade_suggestions():
    """Non-cascade suggestions (e.g. RELEVEL, MERGE_DUPLICATE) must not
    trigger CascadeReviewPolicy.
    """
    from rumil.models import Suggestion

    class _FakeDB:
        async def get_pending_suggestions(self):
            return [
                Suggestion(
                    suggestion_type=SuggestionType.RELEVEL,
                    target_page_id="unrelated",
                ),
                Suggestion(
                    suggestion_type=SuggestionType.MERGE_DUPLICATE,
                    target_page_id="also-unrelated",
                ),
            ]

    policy = CascadeReviewPolicy(db=_FakeDB())  # type: ignore[arg-type]
    state = QuestionState(
        question_id="q",
        budget_remaining=10,
        iteration=0,
        consideration_count=0,
        child_question_count=0,
        source_count=0,
        view=None,
        missing_credence_page_ids=[],
        missing_importance_item_ids=[],
        unjudged_child_question_ids=[],
        recent_call_types=[],
    )
    assert await policy.decide(state) is None


async def test_cascade_orchestrator_registered_in_factory(mocker):
    """Smoke check: the factory dispatches variant='cascade' to
    CascadeOrchestrator without raising.
    """
    from rumil.orchestrators import Orchestrator
    from rumil.settings import override_settings

    fake_db = mocker.MagicMock()
    fake_db.get_budget = AsyncMock(return_value=(100, 0))
    fake_db.budget_remaining = AsyncMock(return_value=100)

    with override_settings(prioritizer_variant="cascade"):
        orch = Orchestrator(fake_db)

    assert isinstance(orch, CascadeOrchestrator)


async def test_cascade_orchestrator_doesnt_ping_pong_same_target(
    tmp_db: DB, patched_assess, mocker
):
    """If assessing a target re-creates a cascade with the SAME target
    (pathological case), the orchestrator must not loop forever on it.
    """
    upstream = _claim("upstream", credence=8)
    dep = _claim("dep")
    await tmp_db.save_page(upstream)
    await tmp_db.save_page(dep)
    await _link_depends_on(tmp_db, dep, upstream)

    await check_cascades(tmp_db, upstream.id, {"credence": (8, 4)})

    async def _assess_and_re_cascade(question_id, db, **kwargs):
        ok = await db.consume_budget(1)
        if not ok:
            return None
        if question_id == dep.id:
            await check_cascades(db, upstream.id, {"credence": (4, 8)})
        return "call-x"

    mocker.patch(
        "rumil.orchestrators.cascade.assess_question",
        side_effect=_assess_and_re_cascade,
    )

    orch = CascadeOrchestrator(tmp_db, max_iterations=5)
    await orch.run(upstream.id)

    dep_assess_count = sum(1 for tgt in patched_assess if tgt == dep.id)
    assert dep_assess_count <= 1
