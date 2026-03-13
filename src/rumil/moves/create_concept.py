"""CREATE_CONCEPT move: create a defined term or distinction."""

from rumil.database import DB
from rumil.models import Call, MoveType, PageLayer, PageType
from rumil.moves.base import CreatePagePayload, MoveDef, MoveResult, create_page


async def execute(payload: CreatePagePayload, call: Call, db: DB) -> MoveResult:
    return await create_page(payload, call, db, PageType.CONCEPT, PageLayer.SQUIDGY)


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
