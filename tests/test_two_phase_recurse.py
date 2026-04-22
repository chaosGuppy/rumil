"""Recurse → child-prioritiser invariants.

V2: a recurse dispatch transfers budget into a child prioritiser in the
shared registry (not spawning a child orchestrator). These tests pin
the per-node actor contract: the target question gets its own
``QuestionPrioritiser`` (or ``ClaimPrioritiser``) with the requested
budget applied as its ``_budget_cap``.
"""

import pytest

from rumil.calls.common import RunCallResult
from rumil.constants import MIN_TWOPHASE_BUDGET
from rumil.models import (
    CallType,
    Dispatch,
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
from rumil.orchestrators.two_phase import TwoPhaseOrchestrator
from rumil.prioritisers.claim_prioritiser import ClaimPrioritiser
from rumil.prioritisers.question_prioritiser import QuestionPrioritiser


def _scout_dispatch(question_id: str, reason: str = "") -> Dispatch:
    return Dispatch(
        call_type=CallType.FIND_CONSIDERATIONS,
        payload=ScoutDispatchPayload(
            question_id=question_id,
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
async def test_recurse_dispatch_creates_child_question_prioritiser(
    tmp_db, question_page, child_question_page, prio_harness
):
    """A RecurseDispatchPayload → child QuestionPrioritiser in the registry with matching budget_cap."""
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

    registry = tmp_db.prioritiser_registry()
    child = await registry.get(child_question_page.id)
    assert child is not None, "child prioritiser was not created for recurse target"
    assert isinstance(child, QuestionPrioritiser)
    assert child._budget_cap == MIN_TWOPHASE_BUDGET


@pytest.mark.asyncio
async def test_recurse_claim_dispatch_creates_claim_prioritiser(
    tmp_db, question_page, prio_harness
):
    """A RecurseClaimDispatchPayload → child ClaimPrioritiser with matching budget_cap."""
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

    registry = tmp_db.prioritiser_registry()
    child = await registry.get(claim.id)
    assert child is not None, "claim prioritiser was not created for recurse_claim target"
    assert isinstance(child, ClaimPrioritiser)
    assert child._budget_cap == MIN_TWOPHASE_BUDGET


@pytest.mark.asyncio
async def test_recurse_claim_drives_phase1_dispatches(tmp_db, question_page, prio_harness):
    """A claim recurse from a question prio must actually drive phase-1 scouts.

    Pre-V2-port regression: ClaimPrioritiser had no ``_run_round``, so a claim
    recurse created a prioritiser that errored out on first loop iteration and
    the phase-1 scouts were never dispatched. This test pins that the claim's
    round loop actually runs phase-1 and the scouts land in the harness.
    """
    claim = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="Claim to investigate via recurse.",
        headline="Claim to investigate via recurse.",
    )
    await tmp_db.save_page(claim)
    await tmp_db.save_link(
        PageLink(
            from_page_id=claim.id,
            to_page_id=question_page.id,
            link_type=LinkType.CONSIDERATION,
        )
    )

    await tmp_db.init_budget(30)
    recurse_claim = Dispatch(
        call_type=CallType.PRIORITIZATION,
        payload=RecurseClaimDispatchPayload(
            question_id=claim.id,
            budget=MIN_TWOPHASE_BUDGET,
            reason="investigate claim",
        ),
    )
    claim_phase1_scout = Dispatch(
        call_type=CallType.SCOUT_C_HOW_TRUE,
        payload=ScoutDispatchPayload(
            question_id=claim.id,
            max_rounds=1,
            reason="phase1 scout",
        ),
    )
    prio_harness.prio_queue = [
        RunCallResult(dispatches=[_scout_dispatch(question_page.id, "seed")]),
        RunCallResult(dispatches=[recurse_claim]),
        RunCallResult(dispatches=[claim_phase1_scout]),
        RunCallResult(dispatches=[]),
        RunCallResult(dispatches=[]),
        RunCallResult(dispatches=[]),
    ]

    parent = TwoPhaseOrchestrator(tmp_db)
    await parent.run(question_page.id)

    # Dispatch handlers route by payload class, not call_type — the scripted
    # SCOUT_C_HOW_TRUE + ScoutDispatchPayload lands in the FIND_CONSIDERATIONS
    # handler. What matters for this regression is that *some* phase-1 scout
    # dispatched (i.e. a non-ASSESS call on the claim); the last_call branch
    # also runs a terminal ASSESS which we want to exclude.
    claim_scouts = [
        d
        for d in prio_harness.dispatched
        if d.get("question_id") == claim.id and d.get("call_type") != CallType.ASSESS.value
    ]
    assert claim_scouts, (
        "ClaimPrioritiser did not dispatch phase-1 scouts — its round loop was never executed"
    )


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
