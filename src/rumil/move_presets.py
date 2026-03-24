"""Move presets: named mappings from CallType to available moves."""

from collections.abc import Sequence

from rumil.models import CallType, MoveType
from rumil.settings import get_settings

MovePreset = dict[CallType, Sequence[MoveType]]

PRESETS: dict[str, MovePreset] = {
    "default": {
        CallType.FIND_CONSIDERATIONS: [
            MoveType.CREATE_CLAIM,
            MoveType.CREATE_QUESTION,
            MoveType.LINK_CONSIDERATION,
            MoveType.LINK_CHILD_QUESTION,
            MoveType.LOAD_PAGE,
        ],
        CallType.ASSESS: [
            MoveType.CREATE_CLAIM,
            MoveType.CREATE_QUESTION,
            MoveType.CREATE_JUDGEMENT,
            MoveType.LINK_CONSIDERATION,
            MoveType.LINK_CHILD_QUESTION,
            MoveType.LOAD_PAGE,
        ],
        CallType.INGEST: [
            MoveType.CREATE_CLAIM,
            MoveType.CREATE_QUESTION,
            MoveType.LINK_CONSIDERATION,
            MoveType.LINK_CHILD_QUESTION,
            MoveType.LOAD_PAGE,
        ],
        CallType.PRIORITIZATION: [
            MoveType.CREATE_SUBQUESTION,
            MoveType.LINK_CHILD_QUESTION,
        ],
        CallType.SCOUT_SUBQUESTIONS: [
            MoveType.CREATE_CLAIM,
            MoveType.CREATE_SCOUT_QUESTION,
            MoveType.LINK_CONSIDERATION,
            MoveType.LOAD_PAGE,
        ],
        CallType.SCOUT_HYPOTHESES: [
            MoveType.CREATE_CLAIM,
            MoveType.CREATE_SCOUT_QUESTION,
            MoveType.PROPOSE_HYPOTHESIS,
            MoveType.LINK_CONSIDERATION,
            MoveType.LOAD_PAGE,
        ],
        CallType.SCOUT_ESTIMATES: [
            MoveType.CREATE_CLAIM,
            MoveType.CREATE_SCOUT_QUESTION,
            MoveType.LINK_CONSIDERATION,
            MoveType.LOAD_PAGE,
        ],
        CallType.SCOUT_ANALOGIES: [
            MoveType.CREATE_CLAIM,
            MoveType.CREATE_SCOUT_QUESTION,
            MoveType.LINK_CONSIDERATION,
            MoveType.LINK_RELATED,
            MoveType.LOAD_PAGE,
        ],
        CallType.SCOUT_PARADIGM_CASES: [
            MoveType.CREATE_CLAIM,
            MoveType.CREATE_SCOUT_QUESTION,
            MoveType.LINK_CONSIDERATION,
            MoveType.LOAD_PAGE,
        ],
        CallType.SCOUT_FACTS_TO_CHECK: [
            MoveType.CREATE_CLAIM,
            MoveType.CREATE_SCOUT_QUESTION,
            MoveType.LINK_CONSIDERATION,
            MoveType.LOAD_PAGE,
        ],
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
        CallType.ASSESS: [
            MoveType.CREATE_CLAIM,
            MoveType.CREATE_QUESTION,
            MoveType.CREATE_JUDGEMENT,
            MoveType.LINK_CONSIDERATION,
            MoveType.LINK_CHILD_QUESTION,
            MoveType.LOAD_PAGE,
        ],
        CallType.FIND_CONSIDERATIONS: [
            MoveType.CREATE_CLAIM,
            MoveType.CREATE_QUESTION,
            MoveType.LINK_CONSIDERATION,
            MoveType.LINK_CHILD_QUESTION,
            MoveType.LOAD_PAGE,
        ],
        CallType.INGEST: [
            MoveType.CREATE_CLAIM,
            MoveType.CREATE_QUESTION,
            MoveType.LINK_CONSIDERATION,
            MoveType.LINK_CHILD_QUESTION,
            MoveType.LOAD_PAGE,
        ],
        CallType.PRIORITIZATION: [
            MoveType.CREATE_SUBQUESTION,
            MoveType.LINK_CHILD_QUESTION,
        ],
        CallType.SCOUT_SUBQUESTIONS: [
            MoveType.CREATE_CLAIM,
            MoveType.CREATE_SCOUT_QUESTION,
            MoveType.LINK_CONSIDERATION,
            MoveType.LOAD_PAGE,
        ],
        CallType.SCOUT_HYPOTHESES: [
            MoveType.CREATE_CLAIM,
            MoveType.CREATE_SCOUT_QUESTION,
            MoveType.PROPOSE_HYPOTHESIS,
            MoveType.LINK_CONSIDERATION,
            MoveType.LOAD_PAGE,
        ],
        CallType.SCOUT_ESTIMATES: [
            MoveType.CREATE_CLAIM,
            MoveType.CREATE_SCOUT_QUESTION,
            MoveType.LINK_CONSIDERATION,
            MoveType.LOAD_PAGE,
        ],
        CallType.SCOUT_ANALOGIES: [
            MoveType.CREATE_CLAIM,
            MoveType.CREATE_SCOUT_QUESTION,
            MoveType.LINK_CONSIDERATION,
            MoveType.LINK_RELATED,
            MoveType.LOAD_PAGE,
        ],
        CallType.SCOUT_PARADIGM_CASES: [
            MoveType.CREATE_CLAIM,
            MoveType.CREATE_SCOUT_QUESTION,
            MoveType.LINK_CONSIDERATION,
            MoveType.LOAD_PAGE,
        ],
        CallType.SCOUT_FACTS_TO_CHECK: [
            MoveType.CREATE_CLAIM,
            MoveType.CREATE_SCOUT_QUESTION,
            MoveType.LINK_CONSIDERATION,
            MoveType.LOAD_PAGE,
        ],
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


def get_moves_for_call(call_type: CallType) -> Sequence[MoveType]:
    """Look up available moves for a call type from the active preset."""
    preset_name = get_settings().moves_preset
    preset = PRESETS.get(preset_name)
    if preset is None:
        raise ValueError(
            f"Unknown move preset: {preset_name!r}. "
            f"Available presets: {', '.join(sorted(PRESETS))}"
        )
    moves = preset.get(call_type)
    if moves is None:
        raise ValueError(
            f"Preset {preset_name!r} has no entry for call type {call_type.value!r}. "
            f"Add an entry to the {preset_name!r} preset in move_presets.py."
        )
    return moves
