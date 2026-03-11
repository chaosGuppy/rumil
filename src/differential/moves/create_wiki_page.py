"""CREATE_WIKI_PAGE move: create a maintained summary page."""

from differential.models import MoveType, PageLayer, PageType
from differential.moves.base import CreatePagePayload, MoveDef, MoveResult, MoveState, create_page


async def execute(payload: CreatePagePayload, state: MoveState) -> MoveResult:
    return await create_page(payload, state, PageType.WIKI, PageLayer.WIKI)


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
