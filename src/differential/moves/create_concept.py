"""CREATE_CONCEPT move: create a defined term or distinction."""

from differential.models import MoveType, PageLayer, PageType
from differential.moves.base import CreatePagePayload, MoveDef, MoveResult, MoveState, create_page


async def execute(payload: CreatePagePayload, state: MoveState) -> MoveResult:
    return await create_page(payload, state, PageType.CONCEPT, PageLayer.SQUIDGY)


MOVE = MoveDef(
    move_type=MoveType.CREATE_CONCEPT,
    name="create_concept",
    description=(
        "Create a concept — a defined term or distinction that makes other "
        "thinking easier."
    ),
    schema=CreatePagePayload,
    execute=execute,
)
