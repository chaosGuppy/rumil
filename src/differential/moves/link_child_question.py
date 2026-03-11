"""LINK_CHILD_QUESTION move: mark a question as a sub-question of another."""

from pydantic import BaseModel, Field

from differential.models import LinkType, MoveType
from differential.moves.base import MoveDef, MoveResult, MoveState, link_pages


class LinkChildQuestionPayload(BaseModel):
    parent_id: str = Field(description="Page ID of the parent question")
    child_id: str = Field(description="Page ID of the child question (or LAST_CREATED)")
    reasoning: str = Field("", description="Why this is a sub-question")


async def execute(payload: LinkChildQuestionPayload, state: MoveState) -> MoveResult:
    return await link_pages(
        payload.parent_id,
        payload.child_id,
        payload.reasoning,
        state.db,
        LinkType.CHILD_QUESTION,
    )


MOVE = MoveDef(
    move_type=MoveType.LINK_CHILD_QUESTION,
    name="link_child_question",
    description="Mark a question as a sub-question of another question.",
    schema=LinkChildQuestionPayload,
    execute=execute,
)
