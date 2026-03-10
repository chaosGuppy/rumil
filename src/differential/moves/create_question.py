"""CREATE_QUESTION move: create a research question."""

from differential.database import DB
from differential.models import Call, MoveType, PageLayer, PageType
from differential.moves.base import CreatePagePayload, MoveDef, MoveResult, create_page


def execute(payload: CreatePagePayload, call: Call, db: DB) -> MoveResult:
    return create_page(payload, call, db, PageType.QUESTION, PageLayer.SQUIDGY)


MOVE = MoveDef(
    move_type=MoveType.CREATE_QUESTION,
    name="create_question",
    description=(
        "Create a new research question — an open problem for investigation. "
        "Questions form hierarchies via child_question links."
    ),
    schema=CreatePagePayload,
    execute=execute,
)
