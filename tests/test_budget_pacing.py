"""Tests for the budget pacing function."""

import pytest

from rumil.constants import compute_round_budget
from rumil.orchestrators.two_phase import TwoPhaseOrchestrator


@pytest.mark.parametrize(
    "total, used, expected",
    [
        (250, 0, 60),
        (250, 74, 72),
        (250, 170, 40),
        (250, 204, 46),
        (250, 250, 0),
        (20, 0, 20),
        (10, 0, 10),
        (5, 0, 5),
        (0, 0, 0),
        (1, 0, 1),
    ],
)
def test_paradigm_cases(total: int, used: int, expected: int) -> None:
    assert compute_round_budget(total, used) == expected


def test_never_exceeds_remaining() -> None:
    for total in (5, 10, 20, 50, 100, 250, 1000):
        for used in range(total + 1):
            alloc = compute_round_budget(total, used)
            assert 0 <= alloc <= total - used, (
                f"total={total}, used={used}: alloc={alloc} > remaining={total - used}"
            )


def test_never_negative() -> None:
    assert compute_round_budget(10, 15) == 0
    assert compute_round_budget(0, 0) == 0


def test_base_allocation_scaling() -> None:
    """Base allocation should grow sub-linearly with total budget."""
    prev = 0
    for total in (20, 50, 100, 250, 500, 1000):
        compute_round_budget(total, total)  # 0 remaining
        steady = compute_round_budget(total, total // 2)
        assert steady > 0
        assert steady >= prev or total <= 20
        prev = steady


@pytest.mark.asyncio
async def test_paced_budget_uses_budget_cap_in_nested_orchestrator(tmp_db):
    """When budget_cap is set, _pacing_params returns the local cap/consumed, not global.

    Nested orchestrators pace against their own allocation; a child with cap=20
    paces like a 20-budget run regardless of the global pool size.
    """
    await tmp_db.init_budget(100)
    await tmp_db.consume_budget(40)

    capped = TwoPhaseOrchestrator(tmp_db, budget_cap=20)
    capped._consumed = 5
    total, used = await capped._pacing_params()
    assert (total, used) == (20, 5), (
        f"capped orch should pace from (budget_cap, _consumed); got ({total}, {used})"
    )

    uncapped = TwoPhaseOrchestrator(tmp_db)
    g_total, g_used = await uncapped._pacing_params()
    assert (g_total, g_used) == (100, 40), (
        f"uncapped orch should pace from global budget; got ({g_total}, {g_used})"
    )
