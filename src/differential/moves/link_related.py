"""LINK_RELATED move: create a general relation between two pages."""

from pydantic import BaseModel, Field

from differential.models import LinkType, MoveType
from differential.moves.base import MoveDef, MoveResult, MoveState, link_pages


class LinkRelatedPayload(BaseModel):
    from_page_id: str = Field(description="Page ID of the first page")
    to_page_id: str = Field(description="Page ID of the second page")
    reasoning: str = Field("", description="Nature of the relation")


async def execute(payload: LinkRelatedPayload, state: MoveState) -> MoveResult:
    return await link_pages(
        payload.from_page_id,
        payload.to_page_id,
        payload.reasoning,
        state.db,
        LinkType.RELATED,
    )


MOVE = MoveDef(
    move_type=MoveType.LINK_RELATED,
    name="link_related",
    description="Create a general relation between two pages.",
    schema=LinkRelatedPayload,
    execute=execute,
)
