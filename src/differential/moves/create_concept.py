"""CREATE_CONCEPT move: create a defined term or distinction."""

from differential.database import DB
from differential.models import Call, MoveType, PageLayer, PageType
from differential.moves.base import CreatePagePayload, MoveDef, MoveResult, create_page


def execute(payload: CreatePagePayload, call: Call, db: DB) -> MoveResult:
    return create_page(payload, call, db, PageType.CONCEPT, PageLayer.SQUIDGY)


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
