"""CREATE_VIEW_ITEM move: create a scored View item and link it to a View."""

import logging

from pydantic import Field

from rumil.database import DB
from rumil.models import Call, LinkType, MoveType, PageLayer, PageLink, PageType
from rumil.moves.base import (
    MoveDef,
    MoveResult,
    ScoredPagePayload,
    create_page,
)

log = logging.getLogger(__name__)


class CreateViewItemPayload(ScoredPagePayload):
    view_id: str = Field(description="Page ID of the View this item belongs to.")
    section: str = Field(
        description=(
            "Which section of the View this item belongs to. Must be one of "
            "the View's defined sections (e.g. broader_context, confident_views, "
            "live_hypotheses, key_evidence, assessments, key_uncertainties, other)."
        ),
    )
    importance: int = Field(
        description=(
            "1-5 importance score. 5=core to the View (shown in NL summary), "
            "4=important, 3=useful context, 2=noted but not load-bearing, "
            "1=catch-all."
        ),
    )


async def execute(payload: CreateViewItemPayload, call: Call, db: DB) -> MoveResult:
    result = await create_page(
        payload,
        call,
        db,
        PageType.VIEW_ITEM,
        PageLayer.WIKI,
        robustness=payload.robustness,
        robustness_reasoning=payload.robustness_reasoning,
    )
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
            importance=payload.importance,
            section=payload.section,
            position=next_position,
        )
    )
    log.info(
        "Linked view item %s to view %s (section=%s, importance=%d, position=%d)",
        result.created_page_id[:8],
        payload.view_id[:8],
        payload.section,
        payload.importance,
        next_position,
    )
    return result


MOVE = MoveDef(
    move_type=MoveType.CREATE_VIEW_ITEM,
    name="create_view_item",
    description=(
        "Create a View item — an atomic observation within a View page. "
        "The item is scored for robustness and importance, and assigned to a "
        "section of the View. (View items are not themselves claims and do "
        "not carry a credence score; if the observation is actually a sharp "
        "positive assertion, create a claim and cite it from the View item.)"
    ),
    schema=CreateViewItemPayload,
    execute=execute,
)
