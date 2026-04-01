"""Tests for the budget pacing function."""

import pytest

from rumil.constants import compute_round_budget


@pytest.mark.parametrize(
    "total, used, expected",
    [
        (250, 0, 25),
        (250, 74, 65),
        (250, 170, 40),
        (250, 204, 46),
        (250, 250, 0),
        (20, 0, 10),
        (10, 0, 5),
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
        alloc = compute_round_budget(total, total)  # 0 remaining
        steady = compute_round_budget(total, total // 2)
        assert steady > 0
        assert steady >= prev or total <= 20
        prev = steady
