"""
Domain constants for the research workspace.

Centralises numeric thresholds and defaults that govern orchestration,
call loops, and budget allocation so they can be tuned in one place.
"""

import math


MIN_TWOPHASE_BUDGET = 4
LAST_CALL_THRESHOLD = 12

DEFAULT_FRUIT_THRESHOLD = 4
DEFAULT_MAX_ROUNDS = 5
DEFAULT_INGEST_FRUIT_THRESHOLD = 5
DEFAULT_INGEST_MAX_ROUNDS = 5

SMOKE_TEST_MAX_ROUNDS = 1
SMOKE_TEST_INGEST_MAX_ROUNDS = 1


def compute_round_budget(total: int, used: int) -> int:
    """Decide how much budget to allocate to a single prioritization round.

    Three regimes:
    - **Early run** (used < base): ramp up gradually via geometric mean of
      (used+10, base) so the first rounds are conservative.
    - **Steady state** (used >= base): allocate the full base amount.
    - **Endgame** (remaining < 2*F): split remaining roughly in half so the
      last round isn't left with a tiny stub.
    """
    remaining = total - used
    if remaining <= 0:
        return 0
    base = round((total + 50) ** 0.75)
    if used < base:
        f = round(math.sqrt((used + 50) * base))
    else:
        f = base
    if remaining >= 2 * f:
        return f
    elif remaining < f:
        return remaining
    else:
        return remaining // 2
