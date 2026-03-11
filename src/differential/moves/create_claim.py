"""CREATE_CLAIM move: create an assertion with supporting reasoning."""

import logging

from pydantic import Field

from differential.database import DB
from differential.models import (
    Call,
    ConsiderationDirection,
    LinkType,
    MoveType,
    PageLayer,
    PageLink,
    PageType,
)
from differential.moves.base import CreatePagePayload, MoveDef, MoveResult, create_page
from differential.moves.link_consideration import ConsiderationLinkFields

log = logging.getLogger(__name__)


class CreateClaimPayload(CreatePagePayload):
    links: list[ConsiderationLinkFields] = Field(
        default_factory=list,
        description=(
            "Consideration links to create for this claim. Each entry links "
            "the new claim to a question with a direction and strength."
        ),
    )


async def execute(payload: CreateClaimPayload, call: Call, db: DB) -> MoveResult:
    result = await create_page(payload, call, db, PageType.CLAIM, PageLayer.SQUIDGY)
    if not result.created_page_id or not payload.links:
        return result

    for link_spec in payload.links:
        resolved = await db.resolve_page_id(link_spec.question_id)
        if not resolved:
            log.warning(
                "Inline consideration link skipped: question %s not found",
                link_spec.question_id,
            )
            continue

        direction_str = link_spec.direction.lower()
        try:
            direction = ConsiderationDirection(direction_str)
        except ValueError:
            direction = ConsiderationDirection.NEUTRAL

        await db.save_link(PageLink(
            from_page_id=result.created_page_id,
            to_page_id=resolved,
            link_type=LinkType.CONSIDERATION,
            direction=direction,
            strength=link_spec.strength,
            reasoning=link_spec.reasoning,
        ))
        log.info(
            "Inline consideration linked: %s -> %s (%s, %.1f)",
            result.created_page_id[:8], resolved[:8],
            direction_str, link_spec.strength,
        )

    return result


MOVE = MoveDef(
    move_type=MoveType.CREATE_CLAIM,
    name="create_claim",
    description=(
        "Create a new claim — an assertion with supporting reasoning and "
        "epistemic status. The atomic unit of knowledge. Use the links field "
        "to simultaneously link this claim as a consideration on one or more "
        "questions."
    ),
    schema=CreateClaimPayload,
    execute=execute,
)
