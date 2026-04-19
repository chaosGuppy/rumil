"""WRITE_MODEL_BODY move: fill in a pre-created MODEL page with structured content.

Design rationale: a MODEL page is pre-created with an empty body before
the agent loop runs (see `rumil.calls.build_model.BuildModelCall`). The
agent fills it in with one call to `write_model_body`, which writes the
structured Markdown into the page's `content` field via the standard
`update_page_content` mutation-event path. This keeps the MODEL page as a
single unit that can be superseded wholesale on subsequent build_model
calls, while predictions live as separate CLAIM pages linked to the
question via LINK_CONSIDERATION (or as VIEW_ITEM proposals).
"""

import logging

from pydantic import BaseModel, Field

from rumil.database import DB
from rumil.models import Call, MoveType, PageType
from rumil.moves.base import MoveDef, MoveResult

log = logging.getLogger(__name__)


class WriteModelBodyPayload(BaseModel):
    model_page_id: str = Field(
        description=(
            "Page ID of the MODEL page to fill in. Use the ID shown in the "
            "task description for this call."
        ),
    )
    content: str = Field(
        description=(
            "Full Markdown body of the model. Must include the sections "
            "Variables, Relations, Parameters, Predictions, Assumptions, "
            "and Sensitivities (see the build_model prompt for the format). "
            "The MODEL page's headline is set at page creation based on the "
            "scope question; this move only updates the body."
        ),
    )
    robustness: int = Field(
        description=(
            "1-5 robustness score for the model as a whole. Models are "
            "typically robustness 2-3: informed impression to considered "
            "view. A 4-5 model would need empirical grounding or strong "
            "derivation. See preamble for the full rubric."
        ),
    )
    robustness_reasoning: str = Field(
        description=(
            "One sentence on where the remaining uncertainty stems from "
            "(e.g. 'only two of the five relations have empirical backing')."
        ),
    )


async def execute(payload: WriteModelBodyPayload, call: Call, db: DB) -> MoveResult:
    resolved_id = await db.resolve_page_id(payload.model_page_id)
    if not resolved_id:
        return MoveResult(message=f"ERROR: model page {payload.model_page_id} not found.")

    page = await db.get_page(resolved_id)
    if page is None or page.page_type != PageType.MODEL:
        got = page.page_type.value if page else "missing"
        return MoveResult(
            message=(
                f"ERROR: write_model_body target {resolved_id[:8]} is {got}, expected a model page."
            )
        )

    await db.update_page_content(resolved_id, payload.content)
    await db.update_epistemic_score(
        resolved_id,
        robustness=payload.robustness,
        robustness_reasoning=payload.robustness_reasoning,
    )
    log.info(
        "Model body written: %s (%d chars, robustness=%d)",
        resolved_id[:8],
        len(payload.content),
        payload.robustness,
    )
    return MoveResult(
        message=(
            f"Model body written to [{resolved_id[:8]}]. Now emit predictions "
            "as separate claims (or propose_view_item on a view) so they can be "
            "attacked by downstream scouts."
        ),
    )


MOVE = MoveDef(
    move_type=MoveType.WRITE_MODEL_BODY,
    name="write_model_body",
    description=(
        "Fill in the pre-created MODEL page with the structured theoretical "
        "model (variables, relations, parameters, predictions, assumptions, "
        "sensitivities). Call this exactly once per build_model call, before "
        "emitting predictions as separate claims."
    ),
    schema=WriteModelBodyPayload,
    execute=execute,
)
