"""Move registry: collects all MoveDefs."""

from differential.models import MoveType
from differential.moves.base import MoveDef
from differential.moves.create_claim import MOVE as _create_claim
from differential.moves.create_question import MOVE as _create_question
from differential.moves.create_judgement import MOVE as _create_judgement
from differential.moves.create_concept import MOVE as _create_concept
from differential.moves.create_wiki_page import MOVE as _create_wiki_page
from differential.moves.link_consideration import MOVE as _link_consideration
from differential.moves.link_child_question import MOVE as _link_child_question
from differential.moves.link_related import MOVE as _link_related
from differential.moves.supersede_page import MOVE as _supersede_page
from differential.moves.flag_funniness import MOVE as _flag_funniness
from differential.moves.report_duplicate import MOVE as _report_duplicate
from differential.moves.propose_hypothesis import MOVE as _propose_hypothesis
from differential.moves.load_page import MOVE as _load_page

MOVES: dict[MoveType, MoveDef] = {
    m.move_type: m
    for m in [
        _create_claim,
        _create_question,
        _create_judgement,
        _create_concept,
        _create_wiki_page,
        _link_consideration,
        _link_child_question,
        _link_related,
        _supersede_page,
        _flag_funniness,
        _report_duplicate,
        _propose_hypothesis,
        _load_page,
    ]
}
