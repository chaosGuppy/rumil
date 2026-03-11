"""CREATE_CLAIM move: create an assertion with supporting reasoning."""

import logging

from pydantic import BaseModel, Field

from differential.models import (
    ConsiderationDirection,
    LinkType,
    MoveType,
    PageLayer,
    PageLink,
    PageType,
)
from differential.moves.base import CreatePagePayload, MoveDef, MoveResult, MoveState, create_page

log = logging.getLogger(__name__)


class InlineConsiderationLink(BaseModel):
    question_id: str = Field(description="Page ID of the question this claim bears on")
    direction: str = Field("neutral", description="supports, opposes, or neutral")
    strength: float = Field(
        2.5,
        description=(
            "0-5: how strongly this claim bears on the question "
            "(0 = barely relevant, 5 = highly decisive)"
        ),
    )
    reasoning: str = Field(
        "", description="Why this claim bears on the question in this direction"
    )


class CreateClaimPayload(CreatePagePayload):
    links: list[InlineConsiderationLink] = Field(
        default_factory=list,
        description=(
            "Consideration links to create for this claim. Each entry links "
            "the new claim to a question with a direction and strength."
        ),
    )


async def execute(payload: CreateClaimPayload, state: MoveState) -> MoveResult:
    result = await create_page(payload, state, PageType.CLAIM, PageLayer.SQUIDGY)
    if not result.created_page_id or not payload.links:
        return result

    db = state.db
    for link_spec in payload.links:
        question_id = link_spec.question_id
        if question_id == "LAST_CREATED" and state.last_created_id:
            question_id = state.last_created_id
        resolved = await db.resolve_page_id(question_id)
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
