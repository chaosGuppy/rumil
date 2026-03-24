"""Move presets: named mappings from CallType to available moves."""

from collections.abc import Sequence

from rumil.models import CallType, MoveType
from rumil.settings import get_settings

MovePreset = dict[CallType, Sequence[MoveType]]

_ALL_MOVES_EXCEPT_JUDGEMENT = [m for m in MoveType if m != MoveType.CREATE_JUDGEMENT]

PRESETS: dict[str, MovePreset] = {
    "default": {
        CallType.SCOUT_SUBQUESTIONS: [
            MoveType.CREATE_CLAIM,
            MoveType.CREATE_QUESTION,
            MoveType.LINK_CONSIDERATION,
            MoveType.LINK_CHILD_QUESTION,
            MoveType.LOAD_PAGE,
        ],
        CallType.SCOUT_HYPOTHESES: [
            MoveType.CREATE_CLAIM,
            MoveType.CREATE_QUESTION,
            MoveType.PROPOSE_HYPOTHESIS,
            MoveType.LINK_CONSIDERATION,
            MoveType.LINK_CHILD_QUESTION,
            MoveType.LOAD_PAGE,
        ],
        CallType.SCOUT_ESTIMATES: [
            MoveType.CREATE_CLAIM,
            MoveType.CREATE_QUESTION,
            MoveType.LINK_CONSIDERATION,
            MoveType.LINK_CHILD_QUESTION,
            MoveType.LOAD_PAGE,
        ],
        CallType.SCOUT_ANALOGIES: [
            MoveType.CREATE_CLAIM,
            MoveType.CREATE_QUESTION,
            MoveType.LINK_CONSIDERATION,
            MoveType.LINK_CHILD_QUESTION,
            MoveType.LINK_RELATED,
            MoveType.LOAD_PAGE,
        ],
        CallType.SCOUT_PARADIGM_CASES: [
            MoveType.CREATE_CLAIM,
            MoveType.CREATE_QUESTION,
            MoveType.LINK_CONSIDERATION,
            MoveType.LINK_CHILD_QUESTION,
            MoveType.LOAD_PAGE,
        ],
        CallType.SCOUT_FACTS_TO_CHECK: [
            MoveType.CREATE_CLAIM,
            MoveType.CREATE_QUESTION,
            MoveType.LINK_CONSIDERATION,
            MoveType.LINK_CHILD_QUESTION,
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
        CallType.FIND_CONSIDERATIONS: _ALL_MOVES_EXCEPT_JUDGEMENT,
        CallType.INGEST: _ALL_MOVES_EXCEPT_JUDGEMENT,
        CallType.PRIORITIZATION: _ALL_MOVES_EXCEPT_JUDGEMENT,
        CallType.SCOUT_SUBQUESTIONS: [
            MoveType.CREATE_CLAIM,
            MoveType.CREATE_QUESTION,
            MoveType.LINK_CONSIDERATION,
            MoveType.LINK_CHILD_QUESTION,
            MoveType.LOAD_PAGE,
        ],
        CallType.SCOUT_HYPOTHESES: [
            MoveType.CREATE_CLAIM,
            MoveType.CREATE_QUESTION,
            MoveType.PROPOSE_HYPOTHESIS,
            MoveType.LINK_CONSIDERATION,
            MoveType.LINK_CHILD_QUESTION,
            MoveType.LOAD_PAGE,
        ],
        CallType.SCOUT_ESTIMATES: [
            MoveType.CREATE_CLAIM,
            MoveType.CREATE_QUESTION,
            MoveType.LINK_CONSIDERATION,
            MoveType.LINK_CHILD_QUESTION,
            MoveType.LOAD_PAGE,
        ],
        CallType.SCOUT_ANALOGIES: [
            MoveType.CREATE_CLAIM,
            MoveType.CREATE_QUESTION,
            MoveType.LINK_CONSIDERATION,
            MoveType.LINK_CHILD_QUESTION,
            MoveType.LINK_RELATED,
            MoveType.LOAD_PAGE,
        ],
        CallType.SCOUT_PARADIGM_CASES: [
            MoveType.CREATE_CLAIM,
            MoveType.CREATE_QUESTION,
            MoveType.LINK_CONSIDERATION,
            MoveType.LINK_CHILD_QUESTION,
            MoveType.LOAD_PAGE,
        ],
        CallType.SCOUT_FACTS_TO_CHECK: [
            MoveType.CREATE_CLAIM,
            MoveType.CREATE_QUESTION,
            MoveType.LINK_CONSIDERATION,
            MoveType.LINK_CHILD_QUESTION,
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
