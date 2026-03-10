"""CREATE_WIKI_PAGE move: create a maintained summary page."""

from differential.database import DB
from differential.models import Call, MoveType, PageLayer, PageType
from differential.moves.base import CreatePagePayload, MoveDef, MoveResult, create_page


def execute(payload: CreatePagePayload, call: Call, db: DB) -> MoveResult:
    return create_page(payload, call, db, PageType.WIKI, PageLayer.WIKI)


MOVE = MoveDef(
    move_type=MoveType.CREATE_WIKI_PAGE,
    name="create_wiki_page",
    description=(
        "Create a wiki page — a maintained, living summary of current "
        "understanding on a topic."
    ),
    schema=CreatePagePayload,
    execute=execute,
)
