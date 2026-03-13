"""LINK_CONSIDERATION move: link a claim to a question as a consideration."""

import logging

from pydantic import BaseModel, Field

from rumil.database import DB
from rumil.models import (
    Call,
    LinkRole,
    LinkType,
    MoveType,
    PageLink,
)
from rumil.moves.base import MoveDef, MoveResult

log = logging.getLogger(__name__)


class ConsiderationLinkFields(BaseModel):
    question_id: str = Field(description="Page ID of the question")
    strength: float = Field(
        2.5,
        description="0-5: how strongly this claim bears on the question (0 = barely relevant, 5 = highly decisive)",
    )
    reasoning: str = Field(
        "", description="Why this claim bears on the question"
    )
    role: LinkRole = Field(
        LinkRole.STRUCTURAL,
        description=(
            "Link role: 'direct' = this claim directly bears on the answer; "
            "'structural' = this claim frames what evidence/angles to explore."
        ),
    )


class LinkConsiderationPayload(ConsiderationLinkFields):
    claim_id: str = Field(description="Page ID of the claim (or LAST_CREATED)")


async def execute(payload: LinkConsiderationPayload, call: Call, db: DB) -> MoveResult:
    claim_id = await db.resolve_page_id(payload.claim_id)
    question_id = await db.resolve_page_id(payload.question_id)
    if not claim_id or not question_id:
        log.warning(
            "LINK_CONSIDERATION skipped: claim_id=%s, question_id=%s",
            claim_id, question_id,
        )
        return MoveResult("Link skipped — page IDs not found.")

    link = PageLink(
        from_page_id=claim_id,
        to_page_id=question_id,
        link_type=LinkType.CONSIDERATION,
        strength=payload.strength,
        reasoning=payload.reasoning,
        role=payload.role,
    )
    await db.save_link(link)
    log.info(
        "Consideration linked: %s -> %s (%.1f)",
        claim_id[:8], question_id[:8], payload.strength,
    )
    return MoveResult("Done.")


MOVE = MoveDef(
    move_type=MoveType.LINK_CONSIDERATION,
    name="link_consideration",
    description=(
        "Link a claim to a question as a consideration with a strength "
        "rating indicating how strongly it bears on the question."
    ),
    schema=LinkConsiderationPayload,
    execute=execute,
)
