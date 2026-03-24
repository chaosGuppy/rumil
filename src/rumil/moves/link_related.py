"""LINK_RELATED move: create a general relation between two pages."""

import logging

from pydantic import BaseModel, Field

from rumil.database import DB
from rumil.models import Call, LinkType, MoveType, PageType
from rumil.moves.base import MoveDef, MoveResult, link_pages, supersede_old_judgements

log = logging.getLogger(__name__)


class LinkRelatedPayload(BaseModel):
    from_page_id: str = Field(description="Page ID of the first page")
    to_page_id: str = Field(description="Page ID of the second page")
    reasoning: str = Field("", description="Nature of the relation")


async def execute(payload: LinkRelatedPayload, call: Call, db: DB) -> MoveResult:
    result = await link_pages(
        payload.from_page_id,
        payload.to_page_id,
        payload.reasoning,
        db,
        LinkType.RELATED,
    )

    from_id = await db.resolve_page_id(payload.from_page_id)
    to_id = await db.resolve_page_id(payload.to_page_id)
    if from_id and to_id:
        from_page = await db.get_page(from_id)
        to_page = await db.get_page(to_id)
        if (
            from_page
            and from_page.page_type == PageType.JUDGEMENT
            and to_page
            and to_page.page_type == PageType.QUESTION
        ):
            await supersede_old_judgements(from_id, to_id, db)

    return result


MOVE = MoveDef(
    move_type=MoveType.LINK_RELATED,
    name="link_related",
    description="Create a general relation between two pages.",
    schema=LinkRelatedPayload,
    execute=execute,
)
