"""PROPOSE_VIEW_ITEM move: propose an unscored View item for later triage."""

import logging

from pydantic import Field

from rumil.database import DB
from rumil.models import Call, LinkType, MoveType, PageLayer, PageLink, PageType
from rumil.moves.base import (
    CreatePagePayload,
    MoveDef,
    MoveResult,
    create_page,
)

log = logging.getLogger(__name__)


class ProposeViewItemPayload(CreatePagePayload):
    view_id: str = Field(description="Page ID of the View to propose this item for.")
    section: str = Field(
        default="other",
        description=(
            "Suggested section for this item. The next assess call will confirm or reassign it."
        ),
    )


async def execute(payload: ProposeViewItemPayload, call: Call, db: DB) -> MoveResult:
    result = await create_page(payload, call, db, PageType.VIEW_ITEM, PageLayer.WIKI)
    if not result.created_page_id:
        return result

    existing_links = await db.get_links_from(payload.view_id)
    section_positions = [
        link.position or 0
        for link in existing_links
        if link.link_type == LinkType.VIEW_ITEM and link.section == payload.section
    ]
    next_position = max(section_positions, default=-1) + 1

    await db.save_link(
        PageLink(
            from_page_id=payload.view_id,
            to_page_id=result.created_page_id,
            link_type=LinkType.VIEW_ITEM,
            importance=None,
            section=payload.section,
            position=next_position,
        )
    )
    log.info(
        "Proposed view item %s for view %s (section=%s, unscored)",
        result.created_page_id[:8],
        payload.view_id[:8],
        payload.section,
    )
    return result


MOVE = MoveDef(
    move_type=MoveType.PROPOSE_VIEW_ITEM,
    name="propose_view_item",
    description=(
        "Propose an item for inclusion in a View page. The item is created "
        "without an importance score — the next assess call will triage it."
    ),
    schema=ProposeViewItemPayload,
    execute=execute,
)
