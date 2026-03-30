"""PROPOSE_CONCEPT move: propose a concept candidate for assessment."""

from pydantic import BaseModel, Field

from rumil.database import DB
from rumil.models import Call, MoveType, Page, PageLayer, PageType, Workspace
from rumil.moves.base import (
    MoveDef,
    MoveResult,
    extract_and_link_citations,
    write_page_file,
)


class ProposeConceptPayload(BaseModel):
    headline: str = Field(
        description=(
            "10-15 word headline (20 word ceiling). A sharp, self-contained label "
            "for the concept or distinction being proposed."
        )
    )
    content: str = Field(
        description=(
            "Full definition and explanation of the concept. Include: what distinction "
            "it draws, why it is useful for the research, and which parts of the "
            "investigation it might clarify."
        )
    )
    credence: int = Field(5, description="1-9 credence that this concept is useful")
    robustness: int = Field(1, description="1-5 robustness of the concept's value assessment")


async def execute(payload: ProposeConceptPayload, call: Call, db: DB) -> MoveResult:
    page = Page(
        page_type=PageType.CONCEPT,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.CONCEPT_STAGING,
        content=payload.content,
        headline=payload.headline,
        credence=payload.credence,
        robustness=payload.robustness,
        provenance_model="claude-opus-4-6",
        provenance_call_type=call.call_type.value,
        provenance_call_id=call.id,
        extra={
            "stage": "proposed",
            "score": None,
            "screening_passed": None,
            "promoted": False,
            "assessment_rounds": [],
        },
    )
    await db.save_page(page)
    write_page_file(page)
    try:
        await extract_and_link_citations(
            page.id,
            page.content,
            db,
        )
    except Exception:
        pass
    return MoveResult(
        message=f"Proposed concept [{page.id[:8]}]: {payload.headline}",
        created_page_id=page.id,
    )


MOVE = MoveDef(
    move_type=MoveType.PROPOSE_CONCEPT,
    name="propose_concept",
    description=(
        "Propose a concept or distinction for assessment. The concept will be "
        "evaluated before entering the research workspace — it is not immediately "
        "visible to other research calls."
    ),
    schema=ProposeConceptPayload,
    execute=execute,
)
