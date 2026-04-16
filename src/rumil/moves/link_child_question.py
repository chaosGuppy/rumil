"""LINK_CHILD_QUESTION move: mark a question as a sub-question of another."""

from pydantic import BaseModel, Field, model_validator

from rumil.database import DB
from rumil.models import Call, LinkRole, LinkType, MoveType
from rumil.moves.base import MoveDef, MoveResult, link_pages


class ChildQuestionLinkFields(BaseModel):
    parent_id: str = Field(description="Page ID of the parent question")

    @model_validator(mode="before")
    @classmethod
    def _accept_question_id(cls, data: object) -> object:
        if isinstance(data, dict) and "question_id" in data and "parent_id" not in data:
            data = dict(data)
            data["parent_id"] = data.pop("question_id")
        return data

    reasoning: str = Field("", description="Why this is a sub-question")
    role: LinkRole = Field(
        LinkRole.STRUCTURAL,
        description=(
            "Link role: 'direct' = answering this sub-question directly "
            "answers the parent; 'structural' = this sub-question frames "
            "what evidence/angles to explore."
        ),
    )
    impact_on_parent_question: int | None = Field(
        None,
        description=(
            "0-10 estimate of how much answering this question would help "
            "answer the parent question. Higher = more decision-relevant."
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
        impact_on_parent_question=payload.impact_on_parent_question,
    )


MOVE = MoveDef(
    move_type=MoveType.LINK_CHILD_QUESTION,
    name="link_child_question",
    description="Mark a question as a sub-question of another question.",
    schema=LinkChildQuestionPayload,
    execute=execute,
)
