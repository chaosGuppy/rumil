"""
Domain constants for the research workspace.

Centralises numeric thresholds and defaults that govern orchestration,
call loops, and budget allocation so they can be tuned in one place.
"""

import math

MIN_TWOPHASE_BUDGET = 4
MIN_GLOBAL_PRIO_BUDGET = 3
MIN_EXPERIMENTAL_INITIAL_PRIO_BUDGET = 10
MAX_PROPAGATION_REASSESS = 7
LAST_CALL_THRESHOLD = 12

DEFAULT_FRUIT_THRESHOLD = 4
DEFAULT_MAX_ROUNDS = 5
DEFAULT_INGEST_FRUIT_THRESHOLD = 5
DEFAULT_INGEST_MAX_ROUNDS = 5

SMOKE_TEST_MAX_ROUNDS = 1
SMOKE_TEST_INGEST_MAX_ROUNDS = 1

DEFAULT_VIEW_SECTIONS: list[str] = [
    "broader_context",
    "confident_views",
    "live_hypotheses",
    "key_evidence",
    "assessments",
    "key_uncertainties",
    "other",
]


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
    f = round(math.sqrt((used + 50) * base)) if used < base else base
    if remaining >= 2 * f:
        return f
    elif remaining < f:
        return remaining
    else:
        return remaining // 2
