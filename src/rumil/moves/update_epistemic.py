"""UPDATE_EPISTEMIC move: update credence/robustness scores on an existing page."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from rumil.context import format_page
from rumil.database import DB
from rumil.models import Call, MoveType, PageDetail, PageType
from rumil.moves.base import MoveDef, MoveResult

if TYPE_CHECKING:
    from rumil.moves.base import MoveState

log = logging.getLogger(__name__)


class UpdateEpistemicPayload(BaseModel):
    page_id: str = Field(description="Page ID of the page to update")
    credence: int = Field(description="1-9 credence score (probability bucket)")
    robustness: int = Field(description="1-5 robustness score (resilience of view)")
    reasoning: str = Field(description="Why this update is warranted")


async def _context_check(
    payload: UpdateEpistemicPayload, state: MoveState
) -> MoveResult | None:
    """Check whether the source judgement for current scores is in context.

    If the LLM hasn't seen the judgement that established the current scores,
    load it and ask for confirmation before applying the update.
    """
    page_id = await state.db.resolve_page_id(payload.page_id)
    if not page_id:
        return None

    score_entry, source_judgement = await state.db.get_epistemic_score_source(page_id)

    if score_entry is None:
        return None

    if score_entry["call_id"] == state.call.id:
        return None

    if source_judgement is None:
        return None

    if source_judgement.id in state.context_page_ids:
        return None

    state.context_page_ids.add(source_judgement.id)
    formatted = await format_page(
        source_judgement, PageDetail.CONTENT, db=state.db
    )
    return MoveResult(
        f"Before updating scores on [{page_id[:8]}], please review the "
        f"judgement that established the current scores "
        f"(C{score_entry['credence']}/R{score_entry['robustness']}):\n\n"
        f"**[{source_judgement.id[:8]}] {source_judgement.headline}**\n\n"
        f"{formatted}\n\n"
        f"Reasoning for current scores: "
        f"{score_entry.get('reasoning') or '(none)'}\n\n"
        f"If you still want to update the scores after reviewing, "
        f"call update_epistemic again with the same or modified values."
    )


async def execute(payload: UpdateEpistemicPayload, call: Call, db: DB) -> MoveResult:
    page_id = await db.resolve_page_id(payload.page_id)
    if not page_id:
        return MoveResult(f"Could not resolve page ID: {payload.page_id}")
    page = await db.get_page(page_id)
    if page and page.page_type == PageType.QUESTION:
        return MoveResult("Cannot update epistemic scores on a question page.")

    source_page_id = await db.get_latest_judgement_for_call(call.id)

    await db.save_epistemic_score(
        page_id,
        call.id,
        payload.credence,
        payload.robustness,
        payload.reasoning,
        source_page_id=source_page_id,
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
    context_check=_context_check,
)
