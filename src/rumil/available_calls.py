"""Available-calls presets: named configurations for two-phase orchestrator scout and dispatch types."""

from collections.abc import Sequence
from dataclasses import dataclass

from rumil.models import CallType
from rumil.settings import get_settings


@dataclass(frozen=True)
class AvailableCallsPreset:
    """Which call types are available in each phase of the two-phase orchestrator."""

    initial_prioritization_scouts: Sequence[CallType]
    main_phase_prioritization_dispatch: Sequence[CallType]
    claim_phase1_scouts: Sequence[CallType] = ()
    claim_phase2_dispatch: Sequence[CallType] = ()


AVAILABLE_CALLS_PRESETS: dict[str, AvailableCallsPreset] = {
    "default": AvailableCallsPreset(
        initial_prioritization_scouts=[
            CallType.SCOUT_SUBQUESTIONS,
            CallType.SCOUT_ESTIMATES,
            CallType.SCOUT_HYPOTHESES,
            CallType.SCOUT_ANALOGIES,
            CallType.SCOUT_PARADIGM_CASES,
            CallType.SCOUT_FACTCHECKS,
            CallType.SCOUT_WEB_QUESTIONS,
        ],
        main_phase_prioritization_dispatch=[
            CallType.FIND_CONSIDERATIONS,
            CallType.WEB_RESEARCH,
            CallType.SCOUT_SUBQUESTIONS,
            CallType.SCOUT_ESTIMATES,
            CallType.SCOUT_HYPOTHESES,
            CallType.SCOUT_ANALOGIES,
            CallType.SCOUT_PARADIGM_CASES,
            CallType.SCOUT_FACTCHECKS,
        ],
        claim_phase1_scouts=[
            CallType.SCOUT_C_HOW_TRUE,
            CallType.SCOUT_C_HOW_FALSE,
            CallType.SCOUT_C_CRUXES,
            CallType.SCOUT_C_RELEVANT_EVIDENCE,
            CallType.SCOUT_C_STRESS_TEST_CASES,
            CallType.SCOUT_C_ROBUSTIFY,
        ],
        claim_phase2_dispatch=[
            CallType.FIND_CONSIDERATIONS,
            CallType.WEB_RESEARCH,
            CallType.SCOUT_C_HOW_TRUE,
            CallType.SCOUT_C_HOW_FALSE,
            CallType.SCOUT_C_CRUXES,
            CallType.SCOUT_C_RELEVANT_EVIDENCE,
            CallType.SCOUT_C_STRESS_TEST_CASES,
            CallType.SCOUT_C_ROBUSTIFY,
            CallType.SCOUT_C_STRENGTHEN,
        ],
    ),
    "simple": AvailableCallsPreset(
        initial_prioritization_scouts=[
            CallType.SCOUT_ESTIMATES,
            CallType.SCOUT_ANALOGIES,
            CallType.SCOUT_PARADIGM_CASES,
            CallType.SCOUT_FACTCHECKS,
            CallType.SCOUT_WEB_QUESTIONS,
            CallType.SCOUT_DEEP_QUESTIONS,
        ],
        main_phase_prioritization_dispatch=[
            CallType.FIND_CONSIDERATIONS,
            CallType.WEB_RESEARCH,
            CallType.SCOUT_ESTIMATES,
            CallType.SCOUT_ANALOGIES,
            CallType.SCOUT_PARADIGM_CASES,
            CallType.SCOUT_FACTCHECKS,
            CallType.SCOUT_WEB_QUESTIONS,
            CallType.SCOUT_DEEP_QUESTIONS,
        ],
        claim_phase1_scouts=[
            CallType.SCOUT_C_HOW_TRUE,
            CallType.SCOUT_C_HOW_FALSE,
            CallType.SCOUT_C_CRUXES,
            CallType.SCOUT_C_RELEVANT_EVIDENCE,
            CallType.SCOUT_C_STRESS_TEST_CASES,
            CallType.SCOUT_C_ROBUSTIFY,
        ],
        claim_phase2_dispatch=[
            CallType.FIND_CONSIDERATIONS,
            CallType.WEB_RESEARCH,
            CallType.SCOUT_C_HOW_TRUE,
            CallType.SCOUT_C_HOW_FALSE,
            CallType.SCOUT_C_CRUXES,
            CallType.SCOUT_C_RELEVANT_EVIDENCE,
            CallType.SCOUT_C_STRESS_TEST_CASES,
            CallType.SCOUT_C_ROBUSTIFY,
            CallType.SCOUT_C_STRENGTHEN,
        ],
    ),
    "multi-subquestion": AvailableCallsPreset(
        initial_prioritization_scouts=[
            CallType.SCOUT_ESTIMATES,
            CallType.SCOUT_HYPOTHESES,
            CallType.SCOUT_ANALOGIES,
            CallType.SCOUT_PARADIGM_CASES,
            CallType.SCOUT_FACTCHECKS,
            CallType.SCOUT_WEB_QUESTIONS,
            CallType.SCOUT_DEEP_QUESTIONS,
        ],
        main_phase_prioritization_dispatch=[
            CallType.FIND_CONSIDERATIONS,
            CallType.WEB_RESEARCH,
            CallType.SCOUT_ESTIMATES,
            CallType.SCOUT_HYPOTHESES,
            CallType.SCOUT_ANALOGIES,
            CallType.SCOUT_PARADIGM_CASES,
            CallType.SCOUT_FACTCHECKS,
            CallType.SCOUT_WEB_QUESTIONS,
            CallType.SCOUT_DEEP_QUESTIONS,
        ],
        claim_phase1_scouts=[
            CallType.SCOUT_C_HOW_TRUE,
            CallType.SCOUT_C_HOW_FALSE,
            CallType.SCOUT_C_CRUXES,
            CallType.SCOUT_C_RELEVANT_EVIDENCE,
            CallType.SCOUT_C_STRESS_TEST_CASES,
            CallType.SCOUT_C_ROBUSTIFY,
        ],
        claim_phase2_dispatch=[
            CallType.FIND_CONSIDERATIONS,
            CallType.WEB_RESEARCH,
            CallType.SCOUT_C_HOW_TRUE,
            CallType.SCOUT_C_HOW_FALSE,
            CallType.SCOUT_C_CRUXES,
            CallType.SCOUT_C_RELEVANT_EVIDENCE,
            CallType.SCOUT_C_STRESS_TEST_CASES,
            CallType.SCOUT_C_ROBUSTIFY,
            CallType.SCOUT_C_STRENGTHEN,
        ],
    ),
}


def get_available_calls_preset() -> AvailableCallsPreset:
    """Return the active available-calls preset based on settings."""
    name = get_settings().available_calls
    preset = AVAILABLE_CALLS_PRESETS.get(name)
    if preset is None:
        available = ", ".join(sorted(AVAILABLE_CALLS_PRESETS))
        raise ValueError(f"Unknown available-calls preset: {name!r}. Available: {available}")
    return preset
