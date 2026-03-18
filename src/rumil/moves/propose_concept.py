"""PROPOSE_CONCEPT move: propose a concept candidate for assessment."""

from pydantic import BaseModel, Field

from rumil.database import DB
from rumil.models import Call, MoveType, Page, PageLayer, PageType, Workspace
from rumil.moves.base import MoveDef, MoveResult, write_page_file


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
    epistemic_status: float = Field(2.5, description="0-5 confidence that this concept is useful")
    epistemic_type: str = Field("", description="Nature of uncertainty about this concept's value")


async def execute(payload: ProposeConceptPayload, call: Call, db: DB) -> MoveResult:
    page = Page(
        page_type=PageType.CONCEPT,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.CONCEPT_STAGING,
        content=payload.content,
        headline=payload.headline,
        epistemic_status=payload.epistemic_status,
        epistemic_type=payload.epistemic_type,
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
