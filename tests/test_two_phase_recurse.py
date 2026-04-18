"""Recurse → child-orchestrator-spawn invariants.

The recurse path is the closest analog of what the prioritizer rearch
replaces: today, a recurse dispatch spawns a new ``TwoPhaseOrchestrator``
(or ``ClaimInvestigationOrchestrator``) with its own ``budget_cap``.
These tests pin the current contract so the rearch author can tell when
they've accidentally dropped it.
"""

import pytest

from rumil.calls.common import RunCallResult
from rumil.constants import MIN_TWOPHASE_BUDGET
from rumil.models import (
    CallType,
    Dispatch,
    FindConsiderationsMode,
    LinkType,
    Page,
    PageLayer,
    PageLink,
    PageType,
    RecurseClaimDispatchPayload,
    RecurseDispatchPayload,
    ScoutDispatchPayload,
    Workspace,
)
from rumil.orchestrators.claim_investigation import ClaimInvestigationOrchestrator
from rumil.orchestrators.two_phase import TwoPhaseOrchestrator


def _scout_dispatch(question_id: str, reason: str = "") -> Dispatch:
    return Dispatch(
        call_type=CallType.FIND_CONSIDERATIONS,
        payload=ScoutDispatchPayload(
            question_id=question_id,
            mode=FindConsiderationsMode.ALTERNATE,
            max_rounds=1,
            reason=reason,
        ),
    )


def _patch_init(mocker, cls):
    """Wrap ``cls.__init__`` to capture every instance created."""
    instances: list = []
    original_init = cls.__init__

    def capturing_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        instances.append(
            {
                "instance": self,
                "budget_cap": kwargs.get("budget_cap"),
            }
        )

    mocker.patch.object(cls, "__init__", capturing_init)
    return instances


@pytest.mark.asyncio
async def test_recurse_dispatch_spawns_child_twophase_with_budget_cap(
    tmp_db, question_page, child_question_page, prio_harness, mocker
):
    """A RecurseDispatchPayload → a new TwoPhaseOrchestrator with the right budget_cap."""
    instances = _patch_init(mocker, TwoPhaseOrchestrator)
    await tmp_db.init_budget(30)
    recurse = Dispatch(
        call_type=CallType.PRIORITIZATION,
        payload=RecurseDispatchPayload(
            question_id=child_question_page.id,
            budget=MIN_TWOPHASE_BUDGET,
            reason="drill into subquestion",
        ),
    )
    prio_harness.prio_queue = [
        RunCallResult(dispatches=[_scout_dispatch(question_page.id, "seed")]),
        RunCallResult(dispatches=[recurse]),
        RunCallResult(dispatches=[]),
        RunCallResult(dispatches=[]),
    ]

    parent = TwoPhaseOrchestrator(tmp_db)
    await parent.run(question_page.id)

    child_entries = [i for i in instances if i["instance"] is not parent]
    assert len(child_entries) >= 1
    assert child_entries[0]["budget_cap"] == MIN_TWOPHASE_BUDGET


@pytest.mark.asyncio
async def test_recurse_claim_dispatch_spawns_claim_investigation_orchestrator(
    tmp_db, question_page, prio_harness, mocker
):
    """A RecurseClaimDispatchPayload → a new ClaimInvestigationOrchestrator with budget_cap."""
    claim = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="Claim under investigation.",
        headline="Claim under investigation.",
    )
    await tmp_db.save_page(claim)
    await tmp_db.save_link(
        PageLink(
            from_page_id=claim.id,
            to_page_id=question_page.id,
            link_type=LinkType.CONSIDERATION,
        )
    )

    claim_instances = _patch_init(mocker, ClaimInvestigationOrchestrator)
    _patch_init(mocker, TwoPhaseOrchestrator)

    await tmp_db.init_budget(30)
    recurse_claim = Dispatch(
        call_type=CallType.PRIORITIZATION,
        payload=RecurseClaimDispatchPayload(
            question_id=claim.id,
            budget=MIN_TWOPHASE_BUDGET,
            reason="investigate claim",
        ),
    )
    prio_harness.prio_queue = [
        RunCallResult(dispatches=[_scout_dispatch(question_page.id, "seed")]),
        RunCallResult(dispatches=[recurse_claim]),
        RunCallResult(dispatches=[]),
        RunCallResult(dispatches=[]),
    ]

    parent = TwoPhaseOrchestrator(tmp_db)
    await parent.run(question_page.id)

    assert len(claim_instances) >= 1
    assert claim_instances[0]["budget_cap"] == MIN_TWOPHASE_BUDGET


@pytest.mark.asyncio
async def test_recurse_with_missing_question_id_skips_cleanly(
    tmp_db, question_page, prio_harness, mocker
):
    """Unresolvable recurse target → no child orchestrator spawned, no exception."""
    instances = _patch_init(mocker, TwoPhaseOrchestrator)
    await tmp_db.init_budget(30)
    recurse = Dispatch(
        call_type=CallType.PRIORITIZATION,
        payload=RecurseDispatchPayload(
            question_id="00000000-0000-0000-0000-000000000000",
            budget=MIN_TWOPHASE_BUDGET,
            reason="bogus target",
        ),
    )
    prio_harness.prio_queue = [
        RunCallResult(dispatches=[_scout_dispatch(question_page.id, "seed")]),
        RunCallResult(dispatches=[recurse]),
        RunCallResult(dispatches=[]),
    ]

    parent = TwoPhaseOrchestrator(tmp_db)
    await parent.run(question_page.id)

    children = [i for i in instances if i["instance"] is not parent]
    assert children == [], f"child orchestrator was spawned for unresolvable id: {children}"


@pytest.mark.asyncio
async def test_recurse_below_min_budget_not_offered_as_tool(tmp_db, question_page, prio_harness):
    """When dispatch_budget < MIN_TWOPHASE_BUDGET, recurse tools must not be offered.

    Main-phase prio sees extra_dispatch_defs=None in that regime (two_phase.py:641-644).
    """
    await tmp_db.init_budget(14)
    prio_harness.prio_queue = [
        RunCallResult(dispatches=[_scout_dispatch(question_page.id, "seed1")]),
        RunCallResult(dispatches=[_scout_dispatch(question_page.id, "seed2")]),
        RunCallResult(dispatches=[_scout_dispatch(question_page.id, "seed3")]),
        RunCallResult(dispatches=[_scout_dispatch(question_page.id, "seed4")]),
        RunCallResult(dispatches=[]),
    ]

    parent = TwoPhaseOrchestrator(tmp_db)
    await parent.run(question_page.id)

    main_phase_calls = [
        c for c in prio_harness.prio_calls if c.get("extra_dispatch_defs") is not None
    ]
    low_budget_calls = [
        c
        for c in prio_harness.prio_calls
        if c.get("dispatch_budget") is not None and c["dispatch_budget"] < MIN_TWOPHASE_BUDGET
    ]
    for c in low_budget_calls:
        assert c.get("extra_dispatch_defs") is None, (
            "recurse was offered when dispatch_budget was below MIN_TWOPHASE_BUDGET: "
            f"dispatch_budget={c['dispatch_budget']}"
        )
    assert low_budget_calls or main_phase_calls, (
        "neither low- nor high-budget main-phase calls observed — scenario did not exercise recurse gating"
    )
