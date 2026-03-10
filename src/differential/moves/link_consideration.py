"""LINK_CONSIDERATION move: link a claim to a question as a consideration."""

from pydantic import BaseModel, Field

from differential.database import DB
from differential.models import (
    Call,
    ConsiderationDirection,
    LinkType,
    MoveType,
    PageLink,
)
from differential.moves.base import MoveDef, MoveResult


class LinkConsiderationPayload(BaseModel):
    claim_id: str = Field(description="Page ID of the claim (or LAST_CREATED)")
    question_id: str = Field(description="Page ID of the question")
    direction: str = Field("neutral", description="supports, opposes, or neutral")
    strength: float = Field(
        2.5,
        description="0-5: how strongly this claim bears on the question (0 = barely relevant, 5 = highly decisive)",
    )
    reasoning: str = Field(
        "", description="Why this claim bears on the question in this direction"
    )


def execute(payload: LinkConsiderationPayload, call: Call, db: DB) -> MoveResult:
    claim_id = db.resolve_page_id(payload.claim_id)
    question_id = db.resolve_page_id(payload.question_id)
    if not claim_id or not question_id:
        print(
            "  [executor] LINK_CONSIDERATION skipped — one or both page IDs not found"
        )
        return MoveResult("Link skipped — page IDs not found.")

    direction_str = payload.direction.lower()
    try:
        direction = ConsiderationDirection(direction_str)
    except ValueError:
        direction = ConsiderationDirection.NEUTRAL

    link = PageLink(
        from_page_id=claim_id,
        to_page_id=question_id,
        link_type=LinkType.CONSIDERATION,
        direction=direction,
        strength=payload.strength,
        reasoning=payload.reasoning,
    )
    db.save_link(link)
    print(
        f"  [~] Consideration: {db.page_label(claim_id)} -> "
        f"{db.page_label(question_id)} ({direction_str})"
    )
    return MoveResult("Done.")


MOVE = MoveDef(
    move_type=MoveType.LINK_CONSIDERATION,
    name="link_consideration",
    description=(
        "Link a claim to a question as a consideration, indicating how the "
        "claim bears on the question (supports, opposes, or neutral) with a "
        "strength rating."
    ),
    schema=LinkConsiderationPayload,
    execute=execute,
)
