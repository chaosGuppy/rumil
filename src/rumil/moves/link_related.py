"""LINK_RELATED move: create a general relation between two pages."""

import logging

from pydantic import BaseModel, Field

from rumil.database import DB
from rumil.models import Call, LinkType, MoveType, PageType
from rumil.moves.base import MoveDef, MoveResult, link_pages

log = logging.getLogger(__name__)


class LinkRelatedPayload(BaseModel):
    from_page_id: str = Field(description="Page ID of the first page")
    to_page_id: str = Field(description="Page ID of the second page")
    reasoning: str = Field("", description="Nature of the relation")


async def _supersede_old_judgements(
    new_judgement_id: str, question_id: str, db: DB,
) -> None:
    """Supersede any existing judgements on a question when a new one is linked."""
    old_judgements = await db.get_judgements_for_question(question_id)
    for old in old_judgements:
        if old.id == new_judgement_id:
            continue
        await db.supersede_page(old.id, new_judgement_id)
        log.info(
            'Superseded old judgement %s with %s on question %s',
            old.id[:8], new_judgement_id[:8], question_id[:8],
        )


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
            from_page and from_page.page_type == PageType.JUDGEMENT
            and to_page and to_page.page_type == PageType.QUESTION
        ):
            await _supersede_old_judgements(from_id, to_id, db)

    return result


MOVE = MoveDef(
    move_type=MoveType.LINK_RELATED,
    name="link_related",
    description="Create a general relation between two pages.",
    schema=LinkRelatedPayload,
    execute=execute,
)
