"""CREATE_CLAIM move: create an assertion with supporting reasoning."""

from differential.database import DB
from differential.models import Call, MoveType, PageLayer, PageType
from differential.moves.base import CreatePagePayload, MoveDef, MoveResult, create_page


def execute(payload: CreatePagePayload, call: Call, db: DB) -> MoveResult:
    return create_page(payload, call, db, PageType.CLAIM, PageLayer.SQUIDGY)


MOVE = MoveDef(
    move_type=MoveType.CREATE_CLAIM,
    name="create_claim",
    description=(
        "Create a new claim — an assertion with supporting reasoning and "
        "epistemic status. The atomic unit of knowledge. Claims are linked to "
        "questions as considerations."
    ),
    schema=CreatePagePayload,
    execute=execute,
)
