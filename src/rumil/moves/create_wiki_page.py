"""CREATE_WIKI_PAGE move: create a maintained summary page."""

from rumil.database import DB
from rumil.models import Call, MoveType, PageLayer, PageType
from rumil.moves.base import MoveDef, MoveResult, ScoredPagePayload, create_page


async def execute(payload: ScoredPagePayload, call: Call, db: DB) -> MoveResult:
    return await create_page(
        payload,
        call,
        db,
        PageType.WIKI,
        PageLayer.WIKI,
        robustness=payload.robustness,
        robustness_reasoning=payload.robustness_reasoning,
    )


MOVE = MoveDef(
    move_type=MoveType.CREATE_WIKI_PAGE,
    name="create_wiki_page",
    description=(
        "Create a wiki page — a maintained, living summary of current understanding on a topic."
    ),
    schema=ScoredPagePayload,
    execute=execute,
)
