"""CREATE_WIKI_PAGE move: create a maintained summary page."""

from rumil.database import DB
from rumil.models import Call, MoveType, PageLayer, PageType
from rumil.moves.base import CreatePagePayload, MoveDef, MoveResult, create_page


async def execute(payload: CreatePagePayload, call: Call, db: DB) -> MoveResult:
    return await create_page(payload, call, db, PageType.WIKI, PageLayer.WIKI)


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
