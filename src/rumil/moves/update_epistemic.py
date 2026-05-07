"""UPDATE_EPISTEMIC move: update credence/robustness scores on an existing page."""

import logging
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field, model_validator

from rumil.context import format_page
from rumil.database import DB, _rows
from rumil.models import Call, MoveType, PageDetail, PageType
from rumil.moves.base import MoveDef, MoveResult

if TYPE_CHECKING:
    from rumil.moves.base import MoveState

log = logging.getLogger(__name__)


class UpdateEpistemicPayload(BaseModel):
    page_id: str = Field(description="Page ID of the page to update")
    credence: int | None = Field(
        default=None,
        description=(
            "1-9 credence score (probability bucket). Only applies to claim "
            "pages — credence is not meaningful for judgements, summaries, "
            "view items, etc. Omit to leave unchanged."
        ),
    )
    credence_reasoning: str | None = Field(
        default=None,
        description=(
            "Why this credence level — what the claim would have to look "
            "like for a higher or lower credence. Required when `credence` "
            "is set."
        ),
    )
    robustness: int | None = Field(
        default=None,
        description=("1-5 robustness score (resilience of view). Omit to leave unchanged."),
    )
    robustness_reasoning: str | None = Field(
        default=None,
        description=(
            "Where the remaining uncertainty stems from and how reducible "
            "it is (e.g. 'would resolve with one clean benchmark run' vs "
            "'inherent — depends on future human behaviour'). Required "
            "when `robustness` is set."
        ),
    )

    @model_validator(mode="after")
    def _check_reasoning(self) -> UpdateEpistemicPayload:
        if self.credence is not None and not (self.credence_reasoning or "").strip():
            raise ValueError("credence_reasoning is required when credence is set")
        if self.robustness is not None and not (self.robustness_reasoning or "").strip():
            raise ValueError("robustness_reasoning is required when robustness is set")
        return self


async def _context_check(payload: UpdateEpistemicPayload, state: MoveState) -> MoveResult | None:
    """Check whether the source judgement for current scores is in context.

    If the LLM hasn't seen the judgement that established the current scores,
    load it and ask for confirmation before applying the update.
    """
    page_id = await state.db.resolve_page_id(payload.page_id)
    if not page_id:
        return None
    page = await state.db.get_page(page_id)
    if page is None:
        return None

    event_types: list[str] = []
    if payload.credence is not None and page.credence is not None:
        event_types.append("set_credence")
    if payload.robustness is not None and page.robustness is not None:
        event_types.append("set_robustness")
    if not event_types:
        return None

    rows = _rows(
        await state.db._execute(
            state.db.client.table("mutation_events")
            .select("payload, created_at")
            .eq("target_id", page_id)
            .eq("run_id", state.db.run_id)
            .in_("event_type", event_types)
            .order("created_at", desc=True)
            .limit(1)
        )
    )
    if not rows:
        return None
    source_page_id = (rows[0].get("payload") or {}).get("source_page_id")
    if not source_page_id:
        return None
    if source_page_id in state.context_page_ids:
        return None

    source_judgement = await state.db.get_page(source_page_id)
    if source_judgement is None:
        return None

    state.context_page_ids.add(source_judgement.id)
    formatted = await format_page(
        source_judgement,
        PageDetail.CONTENT,
        db=state.db,
        track=True,
        track_tags={"source": "epistemic_source_check"},
    )
    score_parts: list[str] = []
    if page.credence is not None:
        score_parts.append(f"C{page.credence}")
    if page.robustness is not None:
        score_parts.append(f"R{page.robustness}")
    current_summary = "/".join(score_parts) if score_parts else "(no prior scores)"

    reasoning_bits: list[str] = []
    if page.credence_reasoning:
        reasoning_bits.append(f"Credence: {page.credence_reasoning}")
    if page.robustness_reasoning:
        reasoning_bits.append(f"Robustness: {page.robustness_reasoning}")
    prior_reasoning = "\n".join(reasoning_bits) or "(none recorded)"

    return MoveResult(
        f"Before updating scores on [{page_id[:8]}], please review the "
        "judgement that established the current scores "
        f"({current_summary}):\n\n"
        f"**[{source_judgement.id[:8]}] {source_judgement.headline}**\n\n"
        f"{formatted}\n\n"
        f"Reasoning for current scores:\n{prior_reasoning}\n\n"
        "If you still want to update the scores after reviewing, "
        "call update_epistemic again with the same or modified values."
    )


async def execute(payload: UpdateEpistemicPayload, call: Call, db: DB) -> MoveResult:
    page_id = await db.resolve_page_id(payload.page_id)
    if not page_id:
        return MoveResult(f"Could not resolve page ID: {payload.page_id}")
    page = await db.get_page(page_id)
    if page and page.page_type == PageType.QUESTION:
        return MoveResult("Cannot update epistemic scores on a question page.")

    if payload.credence is None and payload.robustness is None:
        return MoveResult("Provide at least one of `credence` or `robustness` to update.")

    if payload.credence is not None and page and page.page_type != PageType.CLAIM:
        return MoveResult(
            "Credence only applies to claim pages. To revise the strength of "
            f"a {page.page_type.value} page, update `robustness` instead."
        )

    source_page_id = await db.get_latest_judgement_for_call(call.id)

    await db.update_epistemic_score(
        page_id,
        credence=payload.credence,
        credence_reasoning=payload.credence_reasoning,
        robustness=payload.robustness,
        robustness_reasoning=payload.robustness_reasoning,
        source_page_id=source_page_id,
    )
    updated: list[str] = []
    if payload.credence is not None:
        updated.append(f"C{payload.credence}")
    if payload.robustness is not None:
        updated.append(f"R{payload.robustness}")
    summary = "/".join(updated)
    log.info("Epistemic scores updated: page=%s %s", payload.page_id[:8], summary)
    return MoveResult(f"Epistemic scores updated for {page_id[:8]}: {summary}")


MOVE = MoveDef(
    move_type=MoveType.UPDATE_EPISTEMIC,
    name="update_epistemic",
    description=(
        "Update epistemic scores on an existing page. Credence (1-9 — how "
        "likely is this to be true?) is claim-only; robustness (1-5 — how "
        "solid is this view?) applies to any non-question page. Supply "
        "whichever fields your new information changes; omit the rest. "
        "Each score you set must be accompanied by reasoning."
    ),
    schema=UpdateEpistemicPayload,
    execute=execute,
    context_check=_context_check,
)
