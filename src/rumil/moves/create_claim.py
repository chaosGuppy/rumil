"""CREATE_CLAIM move: create an assertion with supporting reasoning."""

import logging
from collections.abc import Sequence

from pydantic import Field

from rumil.database import DB
from rumil.models import (
    Call,
    LinkType,
    MoveType,
    PageLayer,
    PageLink,
    PageType,
)
from rumil.moves.base import CreatePagePayload, MoveDef, MoveResult, create_page
from rumil.moves.link_consideration import ConsiderationLinkFields

log = logging.getLogger(__name__)


class CreateClaimPayload(CreatePagePayload):
    source_ids: Sequence[str] = Field(
        default_factory=list,
        description="Source page IDs this claim cites. Creates citation links.",
    )
    links: list[ConsiderationLinkFields] = Field(
        default_factory=list,
        description=(
            "Consideration links to create for this claim. Each entry links "
            "the new claim to a question with a strength rating."
        ),
    )


async def execute(payload: CreateClaimPayload, call: Call, db: DB) -> MoveResult:
    result = await create_page(payload, call, db, PageType.CLAIM, PageLayer.SQUIDGY)
    if not result.created_page_id:
        return result

    for link_spec in payload.links:
        resolved = await db.resolve_page_id(link_spec.question_id)
        if not resolved:
            log.warning(
                "Inline consideration link skipped: question %s not found",
                link_spec.question_id,
            )
            continue

        await db.save_link(PageLink(
            from_page_id=result.created_page_id,
            to_page_id=resolved,
            link_type=LinkType.CONSIDERATION,
            strength=link_spec.strength,
            reasoning=link_spec.reasoning,
            role=link_spec.role,
        ))
        log.info(
            "Inline consideration linked: %s -> %s (%.1f)",
            result.created_page_id[:8], resolved[:8], link_spec.strength,
        )

    for sid in payload.source_ids:
        resolved = await db.resolve_page_id(sid)
        if not resolved:
            log.warning(
                "Citation link skipped: source %s not found", sid,
            )
            continue

        await db.save_link(PageLink(
            from_page_id=result.created_page_id,
            to_page_id=resolved,
            link_type=LinkType.CITES,
        ))
        log.info(
            "Citation linked: %s -> %s",
            result.created_page_id[:8], resolved[:8],
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
