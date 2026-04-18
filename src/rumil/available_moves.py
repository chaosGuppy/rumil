"""Available moves: named mappings from CallType to allowed moves per call type."""

from collections.abc import Sequence

from rumil.models import CallType, MoveType
from rumil.settings import get_settings

AvailableMoves = dict[CallType, Sequence[MoveType]]

PRESETS: dict[str, AvailableMoves] = {
    "default": {
        CallType.FIND_CONSIDERATIONS: [
            MoveType.CREATE_CLAIM,
            MoveType.CREATE_QUESTION,
            MoveType.LINK_CONSIDERATION,
            MoveType.LINK_CHILD_QUESTION,
            MoveType.LOAD_PAGE,
            MoveType.UPDATE_EPISTEMIC,
            MoveType.PROPOSE_VIEW_ITEM,
        ],
        CallType.ASSESS: [
            MoveType.CREATE_CLAIM,
            MoveType.CREATE_QUESTION,
            MoveType.CREATE_JUDGEMENT,
            MoveType.LINK_CONSIDERATION,
            MoveType.LINK_CHILD_QUESTION,
            MoveType.LOAD_PAGE,
            MoveType.UPDATE_EPISTEMIC,
        ],
        CallType.INGEST: [
            MoveType.CREATE_CLAIM,
            MoveType.CREATE_QUESTION,
            MoveType.LINK_CONSIDERATION,
            MoveType.LINK_CHILD_QUESTION,
            MoveType.LOAD_PAGE,
        ],
        CallType.PRIORITIZATION: [],
        CallType.SCOUT_SUBQUESTIONS: [
            MoveType.CREATE_CLAIM,
            MoveType.CREATE_SCOUT_QUESTION,
            MoveType.LINK_CONSIDERATION,
            MoveType.LOAD_PAGE,
            MoveType.PROPOSE_VIEW_ITEM,
        ],
        CallType.SCOUT_HYPOTHESES: [
            MoveType.CREATE_CLAIM,
            MoveType.LINK_CONSIDERATION,
            MoveType.LOAD_PAGE,
            MoveType.PROPOSE_VIEW_ITEM,
        ],
        CallType.SCOUT_ESTIMATES: [
            MoveType.CREATE_CLAIM,
            MoveType.CREATE_SCOUT_QUESTION,
            MoveType.LINK_CONSIDERATION,
            MoveType.LOAD_PAGE,
            MoveType.PROPOSE_VIEW_ITEM,
        ],
        CallType.SCOUT_ANALOGIES: [
            MoveType.CREATE_CLAIM,
            MoveType.CREATE_SCOUT_QUESTION,
            MoveType.LINK_CONSIDERATION,
            MoveType.LINK_RELATED,
            MoveType.LOAD_PAGE,
            MoveType.PROPOSE_VIEW_ITEM,
        ],
        CallType.SCOUT_PARADIGM_CASES: [
            MoveType.CREATE_CLAIM,
            MoveType.CREATE_SCOUT_QUESTION,
            MoveType.LINK_CONSIDERATION,
            MoveType.LOAD_PAGE,
            MoveType.PROPOSE_VIEW_ITEM,
        ],
        CallType.SCOUT_FACTCHECKS: [
            MoveType.CREATE_CLAIM,
            MoveType.CREATE_SCOUT_QUESTION,
            MoveType.LINK_CONSIDERATION,
            MoveType.LOAD_PAGE,
            MoveType.PROPOSE_VIEW_ITEM,
        ],
        CallType.SCOUT_WEB_QUESTIONS: [
            MoveType.CREATE_CLAIM,
            MoveType.CREATE_SCOUT_QUESTION,
            MoveType.LINK_CONSIDERATION,
            MoveType.LOAD_PAGE,
            MoveType.PROPOSE_VIEW_ITEM,
        ],
        CallType.SCOUT_DEEP_QUESTIONS: [
            MoveType.CREATE_CLAIM,
            MoveType.CREATE_SCOUT_QUESTION,
            MoveType.LINK_CONSIDERATION,
            MoveType.LOAD_PAGE,
            MoveType.PROPOSE_VIEW_ITEM,
        ],
        CallType.SCOUT_C_HOW_TRUE: [
            MoveType.CREATE_CLAIM,
            MoveType.LINK_CONSIDERATION,
            MoveType.LOAD_PAGE,
            MoveType.PROPOSE_VIEW_ITEM,
        ],
        CallType.SCOUT_C_HOW_FALSE: [
            MoveType.CREATE_CLAIM,
            MoveType.LINK_CONSIDERATION,
            MoveType.LOAD_PAGE,
            MoveType.PROPOSE_VIEW_ITEM,
        ],
        CallType.SCOUT_C_CRUXES: [
            MoveType.CREATE_CLAIM,
            MoveType.CREATE_SCOUT_QUESTION,
            MoveType.LINK_CONSIDERATION,
            MoveType.LOAD_PAGE,
            MoveType.PROPOSE_VIEW_ITEM,
        ],
        CallType.SCOUT_C_RELEVANT_EVIDENCE: [
            MoveType.CREATE_SCOUT_QUESTION,
            MoveType.LINK_CONSIDERATION,
            MoveType.LOAD_PAGE,
            MoveType.PROPOSE_VIEW_ITEM,
        ],
        CallType.SCOUT_C_STRESS_TEST_CASES: [
            MoveType.CREATE_SCOUT_QUESTION,
            MoveType.LINK_CONSIDERATION,
            MoveType.LOAD_PAGE,
            MoveType.PROPOSE_VIEW_ITEM,
        ],
        CallType.SCOUT_C_ROBUSTIFY: [
            MoveType.CREATE_CLAIM,
            MoveType.LINK_VARIANT,
            MoveType.LOAD_PAGE,
            MoveType.PROPOSE_VIEW_ITEM,
        ],
        CallType.SCOUT_C_STRENGTHEN: [
            MoveType.CREATE_CLAIM,
            MoveType.LINK_VARIANT,
            MoveType.LOAD_PAGE,
            MoveType.PROPOSE_VIEW_ITEM,
        ],
        CallType.WEB_RESEARCH: [
            MoveType.CREATE_CLAIM,
            MoveType.LINK_CONSIDERATION,
            MoveType.LOAD_PAGE,
            MoveType.PROPOSE_VIEW_ITEM,
        ],
        CallType.CREATE_VIEW: [
            MoveType.CREATE_VIEW_ITEM,
            MoveType.LOAD_PAGE,
            MoveType.UPDATE_EPISTEMIC,
        ],
        CallType.UPDATE_VIEW: [
            MoveType.LOAD_PAGE,
            MoveType.UPDATE_EPISTEMIC,
        ],
        CallType.ADVERSARIAL_REVIEW: [],
    },
    "judge-on-assess": {
        CallType.ASSESS: [
            MoveType.CREATE_CLAIM,
            MoveType.CREATE_QUESTION,
            MoveType.CREATE_JUDGEMENT,
            MoveType.LINK_CONSIDERATION,
            MoveType.LINK_CHILD_QUESTION,
            MoveType.LOAD_PAGE,
            MoveType.UPDATE_EPISTEMIC,
        ],
        CallType.FIND_CONSIDERATIONS: [
            MoveType.CREATE_CLAIM,
            MoveType.CREATE_QUESTION,
            MoveType.LINK_CONSIDERATION,
            MoveType.LINK_CHILD_QUESTION,
            MoveType.LOAD_PAGE,
            MoveType.UPDATE_EPISTEMIC,
            MoveType.PROPOSE_VIEW_ITEM,
        ],
        CallType.INGEST: [
            MoveType.CREATE_CLAIM,
            MoveType.CREATE_QUESTION,
            MoveType.LINK_CONSIDERATION,
            MoveType.LINK_CHILD_QUESTION,
            MoveType.LOAD_PAGE,
        ],
        CallType.PRIORITIZATION: [],
        CallType.SCOUT_SUBQUESTIONS: [
            MoveType.CREATE_CLAIM,
            MoveType.CREATE_SCOUT_QUESTION,
            MoveType.LINK_CONSIDERATION,
            MoveType.LOAD_PAGE,
            MoveType.PROPOSE_VIEW_ITEM,
        ],
        CallType.SCOUT_HYPOTHESES: [
            MoveType.CREATE_CLAIM,
            MoveType.LINK_CONSIDERATION,
            MoveType.LOAD_PAGE,
            MoveType.PROPOSE_VIEW_ITEM,
        ],
        CallType.SCOUT_ESTIMATES: [
            MoveType.CREATE_CLAIM,
            MoveType.CREATE_SCOUT_QUESTION,
            MoveType.LINK_CONSIDERATION,
            MoveType.LOAD_PAGE,
            MoveType.PROPOSE_VIEW_ITEM,
        ],
        CallType.SCOUT_ANALOGIES: [
            MoveType.CREATE_CLAIM,
            MoveType.CREATE_SCOUT_QUESTION,
            MoveType.LINK_CONSIDERATION,
            MoveType.LINK_RELATED,
            MoveType.LOAD_PAGE,
            MoveType.PROPOSE_VIEW_ITEM,
        ],
        CallType.SCOUT_PARADIGM_CASES: [
            MoveType.CREATE_CLAIM,
            MoveType.CREATE_SCOUT_QUESTION,
            MoveType.LINK_CONSIDERATION,
            MoveType.LOAD_PAGE,
            MoveType.PROPOSE_VIEW_ITEM,
        ],
        CallType.SCOUT_FACTCHECKS: [
            MoveType.CREATE_CLAIM,
            MoveType.CREATE_SCOUT_QUESTION,
            MoveType.LINK_CONSIDERATION,
            MoveType.LOAD_PAGE,
            MoveType.PROPOSE_VIEW_ITEM,
        ],
        CallType.SCOUT_WEB_QUESTIONS: [
            MoveType.CREATE_CLAIM,
            MoveType.CREATE_SCOUT_QUESTION,
            MoveType.LINK_CONSIDERATION,
            MoveType.LOAD_PAGE,
            MoveType.PROPOSE_VIEW_ITEM,
        ],
        CallType.SCOUT_DEEP_QUESTIONS: [
            MoveType.CREATE_CLAIM,
            MoveType.CREATE_SCOUT_QUESTION,
            MoveType.LINK_CONSIDERATION,
            MoveType.LOAD_PAGE,
            MoveType.PROPOSE_VIEW_ITEM,
        ],
        CallType.SCOUT_C_HOW_TRUE: [
            MoveType.CREATE_CLAIM,
            MoveType.LINK_CONSIDERATION,
            MoveType.LOAD_PAGE,
            MoveType.PROPOSE_VIEW_ITEM,
        ],
        CallType.SCOUT_C_HOW_FALSE: [
            MoveType.CREATE_CLAIM,
            MoveType.LINK_CONSIDERATION,
            MoveType.LOAD_PAGE,
            MoveType.PROPOSE_VIEW_ITEM,
        ],
        CallType.SCOUT_C_CRUXES: [
            MoveType.CREATE_CLAIM,
            MoveType.CREATE_SCOUT_QUESTION,
            MoveType.LINK_CONSIDERATION,
            MoveType.LOAD_PAGE,
            MoveType.PROPOSE_VIEW_ITEM,
        ],
        CallType.SCOUT_C_RELEVANT_EVIDENCE: [
            MoveType.CREATE_SCOUT_QUESTION,
            MoveType.LINK_CONSIDERATION,
            MoveType.LOAD_PAGE,
            MoveType.PROPOSE_VIEW_ITEM,
        ],
        CallType.SCOUT_C_STRESS_TEST_CASES: [
            MoveType.CREATE_SCOUT_QUESTION,
            MoveType.LINK_CONSIDERATION,
            MoveType.LOAD_PAGE,
            MoveType.PROPOSE_VIEW_ITEM,
        ],
        CallType.SCOUT_C_ROBUSTIFY: [
            MoveType.CREATE_CLAIM,
            MoveType.LINK_VARIANT,
            MoveType.LOAD_PAGE,
            MoveType.PROPOSE_VIEW_ITEM,
        ],
        CallType.SCOUT_C_STRENGTHEN: [
            MoveType.CREATE_CLAIM,
            MoveType.LINK_VARIANT,
            MoveType.LOAD_PAGE,
            MoveType.PROPOSE_VIEW_ITEM,
        ],
        CallType.WEB_RESEARCH: [
            MoveType.CREATE_CLAIM,
            MoveType.LINK_CONSIDERATION,
            MoveType.LOAD_PAGE,
            MoveType.PROPOSE_VIEW_ITEM,
        ],
        CallType.CREATE_VIEW: [
            MoveType.CREATE_VIEW_ITEM,
            MoveType.LOAD_PAGE,
            MoveType.UPDATE_EPISTEMIC,
        ],
        CallType.UPDATE_VIEW: [
            MoveType.LOAD_PAGE,
            MoveType.UPDATE_EPISTEMIC,
        ],
        CallType.ADVERSARIAL_REVIEW: [],
    },
}


def get_moves_for_call(call_type: CallType) -> Sequence[MoveType]:
    """Look up available moves for a call type from the active preset."""
    preset_name = get_settings().available_moves
    preset = PRESETS.get(preset_name)
    if preset is None:
        raise ValueError(
            f"Unknown available-moves preset: {preset_name!r}. "
            f"Available presets: {', '.join(sorted(PRESETS))}"
        )
    moves = preset.get(call_type)
    if moves is None:
        raise ValueError(
            f"Preset {preset_name!r} has no entry for call type {call_type.value!r}. "
            f"Add an entry to the {preset_name!r} preset in available_moves.py."
        )
    return moves
