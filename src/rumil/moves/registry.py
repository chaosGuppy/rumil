"""Move registry: collects all MoveDefs."""

from rumil.models import MoveType
from rumil.moves.base import MoveDef
from rumil.moves.create_claim import MOVE as _create_claim
from rumil.moves.create_question import (
    MOVE as _create_question,
    PRIORITIZATION_MOVE as _create_subquestion,
    SCOUT_MOVE as _create_scout_question,
)
from rumil.moves.create_judgement import MOVE as _create_judgement
from rumil.moves.create_wiki_page import MOVE as _create_wiki_page
from rumil.moves.link_consideration import MOVE as _link_consideration
from rumil.moves.link_child_question import MOVE as _link_child_question
from rumil.moves.link_related import MOVE as _link_related
from rumil.moves.link_variant import MOVE as _link_variant
from rumil.moves.flag_funniness import MOVE as _flag_funniness
from rumil.moves.report_duplicate import MOVE as _report_duplicate
from rumil.moves.load_page import MOVE as _load_page
from rumil.moves.remove_link import MOVE as _remove_link
from rumil.moves.change_link_role import MOVE as _change_link_role
from rumil.moves.update_epistemic import MOVE as _update_epistemic
from rumil.moves.link_depends_on import MOVE as _link_depends_on
from rumil.moves.create_view_item import MOVE as _create_view_item
from rumil.moves.propose_view_item import MOVE as _propose_view_item

MOVES: dict[MoveType, MoveDef] = {
    m.move_type: m
    for m in [
        _create_claim,
        _create_question,
        _create_scout_question,
        _create_subquestion,
        _create_judgement,
        _create_wiki_page,
        _link_consideration,
        _link_child_question,
        _link_related,
        _link_variant,
        _flag_funniness,
        _report_duplicate,
        _load_page,
        _remove_link,
        _change_link_role,
        _update_epistemic,
        _link_depends_on,
        _create_view_item,
        _propose_view_item,
    ]
}
