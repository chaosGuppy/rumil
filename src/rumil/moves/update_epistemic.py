"""UPDATE_EPISTEMIC move: update credence/robustness scores on an existing page."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from rumil.cascades import check_cascades
from rumil.context import format_page
from rumil.database import DB
from rumil.models import Call, MoveType, PageDetail, PageType
from rumil.moves.base import MoveDef, MoveResult

if TYPE_CHECKING:
    from rumil.moves.base import MoveState

log = logging.getLogger(__name__)


class UpdateEpistemicPayload(BaseModel):
    page_id: str = Field(description="Page ID of the page to update")
    credence: int | None = Field(
        default=None, description="1-9 credence score (probability bucket)"
    )
    robustness: int | None = Field(
        default=None, description="1-5 robustness score (resilience of view)"
    )
    reasoning: str = Field(description="Why this update is warranted")


async def _context_check(
    payload: UpdateEpistemicPayload, state: MoveState
) -> MoveResult | None:
    """Check whether the source judgement for current scores is in context.

    If the LLM hasn't seen the judgement that established the current scores,
    load it and ask for confirmation before applying the update.
    """
    if payload.credence is None and payload.robustness is None:
        return None

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
    formatted = await format_page(source_judgement, PageDetail.CONTENT, db=state.db)
    return MoveResult(
        f"Before updating scores on [{page_id[:8]}], please review the "
        "judgement that established the current scores "
        f"(C{score_entry['credence']}/R{score_entry['robustness']}):\n\n"
        f"**[{source_judgement.id[:8]}] {source_judgement.headline}**\n\n"
        f"{formatted}\n\n"
        "Reasoning for current scores: "
        f"{score_entry.get('reasoning') or '(none)'}\n\n"
        "If you still want to update the scores after reviewing, "
        "call update_epistemic again with the same or modified values."
    )


async def execute(payload: UpdateEpistemicPayload, call: Call, db: DB) -> MoveResult:
    page_id = await db.resolve_page_id(payload.page_id)
    if not page_id:
        return MoveResult(f"Could not resolve page ID: {payload.page_id}")
    page = await db.get_page(page_id)
    if not page:
        return MoveResult(f"Page {payload.page_id} not found.")
    if page.page_type == PageType.QUESTION:
        return MoveResult("Cannot update epistemic scores on a question page.")

    old_credence = page.credence
    old_robustness = page.robustness
    parts: list[str] = []

    if payload.credence is not None and payload.robustness is not None:
        source_page_id = await db.get_latest_judgement_for_call(call.id)
        await db.save_epistemic_score(
            page_id,
            call.id,
            payload.credence,
            payload.robustness,
            payload.reasoning,
            source_page_id=source_page_id,
        )
        parts.append(f"C{payload.credence}/R{payload.robustness}")
        log.info(
            "Epistemic scores updated: page=%s C%d/R%d",
            payload.page_id[:8],
            payload.credence,
            payload.robustness,
        )

    if not parts:
        return MoveResult("No scores provided to update.")

    cascade_changes: dict[str, tuple[object, object]] = {}
    if payload.credence is not None and old_credence is not None:
        cascade_changes["credence"] = (old_credence, payload.credence)
    if payload.robustness is not None and old_robustness is not None:
        cascade_changes["robustness"] = (old_robustness, payload.robustness)
    if cascade_changes:
        suggestions = await check_cascades(
            db,
            page_id,
            cascade_changes,
            call_id=call.id,
        )
        if suggestions:
            parts.append(f"{len(suggestions)} cascade(s) flagged")

    return MoveResult(f"Updated {page_id[:8]}: {', '.join(parts)}")


MOVE = MoveDef(
    move_type=MoveType.UPDATE_EPISTEMIC,
    name="update_epistemic",
    description=(
        "Update epistemic scores on an existing page. Provide "
        "credence+robustness together with reasoning for the update."
    ),
    schema=UpdateEpistemicPayload,
    execute=execute,
    context_check=_context_check,
)
