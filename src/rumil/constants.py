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

FREEFORM_VIEW_SECTIONS: list[str] = [
    "framing_and_interpretation",
    "assertions_and_deductions",
    "research_direction",
    "returns_to_further_research",
]

FREEFORM_VIEW_SECTION_BRIEFS: dict[str, str] = {
    "framing_and_interpretation": (
        "Write the **framing_and_interpretation** section.\n\n"
        "What is this question really about? At a very high level, what is "
        "important to consider when investigating it? Are there frames that "
        "might seem important superficially but, on deeper reflection, should "
        "be deprioritised? Are there ambiguities in the question, and if so, "
        "what are the most helpful resolutions?"
    ),
    "assertions_and_deductions": (
        "Write the **assertions_and_deductions** section.\n\n"
        "What can we state about the answer to this question, and at what "
        "confidence level? Reasoning carefully and deeply from the available "
        "(likely uncertain) evidence, claims, and crystallised knowledge, "
        "what can we derive? Use language that carefully and faithfully "
        "conveys uncertainty, and show how that uncertainty propagates "
        "through your reasoning. Do not introduce meaningless probabilities "
        "over high-level, undefined propositions; only reason in terms of "
        "probabilities when the proposition is precise enough that the "
        "assignment is genuinely meaningful. Otherwise, prefer language like "
        '"confident", "uncertain", "would expect to update upon further '
        'research".'
    ),
    "research_direction": (
        "Write the **research_direction** section.\n\n"
        "What cruxes and key unknowns would, if investigated, most improve "
        "our answer to the question?"
    ),
    "returns_to_further_research": (
        "Write the **returns_to_further_research** section.\n\n"
        "How much reducible uncertainty remains in the question? How far "
        'are we from a "perfect" (i.e. maximally-uncertainty-reduced) '
        "answer? And what does the curve of returns to further research "
        "look like? Is it flat for a long time but spikes at high enough "
        "effort? Does it saturate quickly? Linear for a long time before "
        "saturating? Or something else?"
    ),
}


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
