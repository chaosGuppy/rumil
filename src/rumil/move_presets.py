"""Move presets: named mappings from CallType to available moves."""

from collections.abc import Sequence

from rumil.models import CallType, MoveType
from rumil.settings import get_settings

MovePreset = dict[CallType, Sequence[MoveType]]

_ALL_MOVES_EXCEPT_JUDGEMENT = [m for m in MoveType if m != MoveType.CREATE_JUDGEMENT]

_SCOUT_SUBQUESTIONS_MOVES = [
    MoveType.CREATE_CLAIM,
    MoveType.CREATE_QUESTION,
    MoveType.LINK_CONSIDERATION,
    MoveType.LOAD_PAGE,
]

_SCOUT_HYPOTHESES_MOVES = [
    MoveType.CREATE_CLAIM,
    MoveType.CREATE_QUESTION,
    MoveType.PROPOSE_HYPOTHESIS,
    MoveType.LINK_CONSIDERATION,
    MoveType.LOAD_PAGE,
]

_SCOUT_ESTIMATES_MOVES = [
    MoveType.CREATE_CLAIM,
    MoveType.CREATE_QUESTION,
    MoveType.LINK_CONSIDERATION,
    MoveType.LOAD_PAGE,
]

_SCOUT_ANALOGIES_MOVES = [
    MoveType.CREATE_CLAIM,
    MoveType.CREATE_QUESTION,
    MoveType.LINK_CONSIDERATION,
    MoveType.LINK_RELATED,
    MoveType.LOAD_PAGE,
]

_SCOUT_PARADIGM_CASES_MOVES = [
    MoveType.CREATE_CLAIM,
    MoveType.CREATE_QUESTION,
    MoveType.LINK_CONSIDERATION,
    MoveType.LOAD_PAGE,
]

_SCOUT_FACTS_TO_CHECK_MOVES = [
    MoveType.CREATE_CLAIM,
    MoveType.CREATE_QUESTION,
    MoveType.LINK_CONSIDERATION,
    MoveType.LOAD_PAGE,
]

PRESETS: dict[str, MovePreset] = {
    "default": {
        CallType.SCOUT_SUBQUESTIONS: _SCOUT_SUBQUESTIONS_MOVES,
        CallType.SCOUT_HYPOTHESES: _SCOUT_HYPOTHESES_MOVES,
        CallType.SCOUT_ESTIMATES: _SCOUT_ESTIMATES_MOVES,
        CallType.SCOUT_ANALOGIES: _SCOUT_ANALOGIES_MOVES,
        CallType.SCOUT_PARADIGM_CASES: _SCOUT_PARADIGM_CASES_MOVES,
        CallType.SCOUT_FACTS_TO_CHECK: _SCOUT_FACTS_TO_CHECK_MOVES,
        CallType.SCOUT_CONCEPTS: [
            MoveType.PROPOSE_CONCEPT,
            MoveType.LOAD_PAGE,
        ],
        CallType.WEB_RESEARCH: [
            MoveType.CREATE_CLAIM,
            MoveType.LINK_CONSIDERATION,
            MoveType.LOAD_PAGE,
        ],
    },
    "judge-on-assess": {
        CallType.FIND_CONSIDERATIONS: _ALL_MOVES_EXCEPT_JUDGEMENT,
        CallType.INGEST: _ALL_MOVES_EXCEPT_JUDGEMENT,
        CallType.PRIORITIZATION: _ALL_MOVES_EXCEPT_JUDGEMENT,
        CallType.SCOUT_SUBQUESTIONS: _SCOUT_SUBQUESTIONS_MOVES,
        CallType.SCOUT_HYPOTHESES: _SCOUT_HYPOTHESES_MOVES,
        CallType.SCOUT_ESTIMATES: _SCOUT_ESTIMATES_MOVES,
        CallType.SCOUT_ANALOGIES: _SCOUT_ANALOGIES_MOVES,
        CallType.SCOUT_PARADIGM_CASES: _SCOUT_PARADIGM_CASES_MOVES,
        CallType.SCOUT_FACTS_TO_CHECK: _SCOUT_FACTS_TO_CHECK_MOVES,
        CallType.SCOUT_CONCEPTS: [
            MoveType.PROPOSE_CONCEPT,
            MoveType.LOAD_PAGE,
        ],
        CallType.WEB_RESEARCH: [
            MoveType.CREATE_CLAIM,
            MoveType.LINK_CONSIDERATION,
            MoveType.LOAD_PAGE,
        ],
    },
}


def get_moves_for_call(call_type: CallType) -> Sequence[MoveType] | None:
    """Look up available moves for a call type from the active preset.

    Returns the move list if the preset has an entry for this call type,
    or None if the call type is not in the preset (meaning "use all moves"
    or whatever the call type's own default is).
    """
    preset_name = get_settings().moves_preset
    preset = PRESETS.get(preset_name)
    if preset is None:
        raise ValueError(
            f"Unknown move preset: {preset_name!r}. "
            f"Available presets: {', '.join(sorted(PRESETS))}"
        )
    return preset.get(call_type)
