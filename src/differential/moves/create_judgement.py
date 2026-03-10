"""CREATE_JUDGEMENT move: create a considered position on a question."""

from differential.database import DB
from differential.models import Call, MoveType, PageLayer, PageType
from differential.moves.base import CreatePagePayload, MoveDef, MoveResult, create_page


def execute(payload: CreatePagePayload, call: Call, db: DB) -> MoveResult:
    return create_page(payload, call, db, PageType.JUDGEMENT, PageLayer.SQUIDGY)


MOVE = MoveDef(
    move_type=MoveType.CREATE_JUDGEMENT,
    name="create_judgement",
    description=(
        "Create a judgement — a considered position synthesising the "
        "considerations bearing on a question. Must engage with "
        "considerations on multiple sides. Include key_dependencies and "
        "sensitivity_analysis fields."
    ),
    schema=CreatePagePayload,
    execute=execute,
)
