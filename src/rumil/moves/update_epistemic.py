"""UPDATE_EPISTEMIC move: update credence/robustness scores on an existing page."""

import logging

from pydantic import BaseModel, Field

from rumil.database import DB
from rumil.models import Call, MoveType, PageType
from rumil.moves.base import MoveDef, MoveResult

log = logging.getLogger(__name__)


class UpdateEpistemicPayload(BaseModel):
    page_id: str = Field(description="Page ID of the page to update")
    credence: int = Field(description="1-9 credence score (probability bucket)")
    robustness: int = Field(description="1-5 robustness score (resilience of view)")
    reasoning: str = Field(description="Why this update is warranted")


async def execute(payload: UpdateEpistemicPayload, call: Call, db: DB) -> MoveResult:
    page_id = await db.resolve_page_id(payload.page_id)
    if not page_id:
        return MoveResult(f"Could not resolve page ID: {payload.page_id}")
    page = await db.get_page(page_id)
    if page and page.page_type == PageType.QUESTION:
        return MoveResult("Cannot update epistemic scores on a question page.")
    await db.save_epistemic_score(
        page_id,
        call.id,
        payload.credence,
        payload.robustness,
        payload.reasoning,
    )
    log.info(
        "Epistemic scores updated: page=%s C%d/R%d",
        payload.page_id[:8],
        payload.credence,
        payload.robustness,
    )
    return MoveResult(
        f"Epistemic scores updated for {page_id[:8]}: "
        f"C{payload.credence}/R{payload.robustness}"
    )


MOVE = MoveDef(
    move_type=MoveType.UPDATE_EPISTEMIC,
    name="update_epistemic",
    description=(
        "Update the credence and robustness scores on an existing page. "
        "Use when you have new information or analysis that changes how "
        "confident you are in a claim or how well-grounded the assessment is."
    ),
    schema=UpdateEpistemicPayload,
    execute=execute,
)
