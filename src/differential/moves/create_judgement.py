"""CREATE_JUDGEMENT move: create a considered position on a question."""

from differential.models import MoveType, PageLayer, PageType
from differential.moves.base import CreatePagePayload, MoveDef, MoveResult, MoveState, create_page


async def execute(payload: CreatePagePayload, state: MoveState) -> MoveResult:
    return await create_page(payload, state, PageType.JUDGEMENT, PageLayer.SQUIDGY)


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
