"""LINK_VARIANT move: link a claim to a more robust variation of it."""

import logging

from pydantic import BaseModel, Field

from rumil.database import DB
from rumil.models import Call, LinkType, MoveType
from rumil.moves.base import MoveDef, MoveResult, link_pages

log = logging.getLogger(__name__)


class LinkVariantPayload(BaseModel):
    original_page_id: str = Field(description="Page ID of the original claim")
    variant_page_id: str = Field(description="Page ID of the more robust variant claim")
    reasoning: str = Field(
        "",
        description="What makes the variant more robust than the original",
    )


async def execute(payload: LinkVariantPayload, call: Call, db: DB) -> MoveResult:
    return await link_pages(
        payload.variant_page_id,
        payload.original_page_id,
        payload.reasoning,
        db,
        LinkType.VARIANT,
    )


MOVE = MoveDef(
    move_type=MoveType.LINK_VARIANT,
    name="link_variant",
    description=(
        "Link a newly created claim as a more robust variant of an existing claim. "
        "The variant is a version of the original that trades some precision or scope "
        "for greater defensibility."
    ),
    schema=LinkVariantPayload,
    execute=execute,
)
