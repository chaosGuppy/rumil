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
async def test_paced_budget_uses_pool_when_registered(tmp_db, question_page):
    """When the orchestrator's pool is registered, ``_pacing_params`` returns
    pool-level totals, so pacing scales with the actual shared pool — including
    any sibling contributions — rather than just this orchestrator's slice.
    """
    await tmp_db.init_budget(100)
    await tmp_db.consume_budget(40)

    # Pool registered with a sibling contribution of 30 already consumed by 10.
    # When this orchestrator joins, contributions sum and pacing sees the total.
    await tmp_db.qbp_register(question_page.id, 30)
    await tmp_db.qbp_consume(question_page.id, 10)

    pooled = TwoPhaseOrchestrator(tmp_db)
    pooled.pool_question_id = question_page.id
    total, used = await pooled._pacing_params()
    assert (total, used) == (30, 10), (
        f"pooled orch should pace from (pool.contributed, pool.consumed); got ({total}, {used})"
    )

    uncapped = TwoPhaseOrchestrator(tmp_db)
    g_total, g_used = await uncapped._pacing_params()
    assert (g_total, g_used) == (100, 40), (
        f"orch without a registered pool should pace from global budget; got ({g_total}, {g_used})"
    )
