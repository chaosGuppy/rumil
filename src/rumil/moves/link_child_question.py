"""LINK_CHILD_QUESTION move: mark a question as a sub-question of another."""

from pydantic import BaseModel, Field

from rumil.database import DB
from rumil.models import Call, LinkRole, LinkType, MoveType
from rumil.moves.base import MoveDef, MoveResult, link_pages


class ChildQuestionLinkFields(BaseModel):
    parent_id: str = Field(description="Page ID of the parent question")
    reasoning: str = Field("", description="Why this is a sub-question")
    role: LinkRole = Field(
        LinkRole.STRUCTURAL,
        description=(
            "Link role: 'direct' = answering this sub-question directly "
            "answers the parent; 'structural' = this sub-question frames "
            "what evidence/angles to explore."
        ),
    )


class LinkChildQuestionPayload(ChildQuestionLinkFields):
    child_id: str = Field(description="Page ID of the child question (or LAST_CREATED)")


async def execute(payload: LinkChildQuestionPayload, call: Call, db: DB) -> MoveResult:
    return await link_pages(
        payload.parent_id,
        payload.child_id,
        payload.reasoning,
        db,
        LinkType.CHILD_QUESTION,
        role=payload.role,
    )


MOVE = MoveDef(
    move_type=MoveType.LINK_CHILD_QUESTION,
    name="link_child_question",
    description="Mark a question as a sub-question of another question.",
    schema=LinkChildQuestionPayload,
    execute=execute,
)
