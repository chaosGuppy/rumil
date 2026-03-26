"""Available-calls presets: named configurations for two-phase orchestrator scout and dispatch types."""

from collections.abc import Sequence
from dataclasses import dataclass

from rumil.models import CallType
from rumil.settings import get_settings


@dataclass(frozen=True)
class AvailableCallsPreset:
    """Which call types are available in each phase of the two-phase orchestrator."""

    phase1_scouts: Sequence[CallType]
    phase2_dispatch: Sequence[CallType]


AVAILABLE_CALLS_PRESETS: dict[str, AvailableCallsPreset] = {
    "default": AvailableCallsPreset(
        phase1_scouts=[
            CallType.SCOUT_SUBQUESTIONS,
            CallType.SCOUT_ESTIMATES,
            CallType.SCOUT_HYPOTHESES,
            CallType.SCOUT_ANALOGIES,
            CallType.SCOUT_PARADIGM_CASES,
            CallType.SCOUT_FACTCHECKS,
        ],
        phase2_dispatch=[
            CallType.FIND_CONSIDERATIONS,
            CallType.WEB_RESEARCH,
            CallType.SCOUT_SUBQUESTIONS,
            CallType.SCOUT_ESTIMATES,
            CallType.SCOUT_HYPOTHESES,
            CallType.SCOUT_ANALOGIES,
            CallType.SCOUT_PARADIGM_CASES,
            CallType.SCOUT_FACTCHECKS,
        ],
    ),
    "multi-subquestion": AvailableCallsPreset(
        phase1_scouts=[
            CallType.SCOUT_ESTIMATES,
            CallType.SCOUT_HYPOTHESES,
            CallType.SCOUT_ANALOGIES,
            CallType.SCOUT_PARADIGM_CASES,
            CallType.SCOUT_FACTCHECKS,
            CallType.SCOUT_WEB_QUESTIONS,
            CallType.SCOUT_DEEP_QUESTIONS,
        ],
        phase2_dispatch=[
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
    ),
}


def get_available_calls_preset() -> AvailableCallsPreset:
    """Return the active available-calls preset based on settings."""
    name = get_settings().available_calls
    preset = AVAILABLE_CALLS_PRESETS.get(name)
    if preset is None:
        available = ", ".join(sorted(AVAILABLE_CALLS_PRESETS))
        raise ValueError(
            f"Unknown available-calls preset: {name!r}. Available: {available}"
        )
    return preset
