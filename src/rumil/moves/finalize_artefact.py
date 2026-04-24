"""FINALIZE_ARTEFACT: promote the latest artefact from hidden to visible.

This is the exit gate of the generative workflow: all the iteration lineage
(spec items, superseded artefact drafts, critique judgements) stays hidden;
only the final artefact surfaces to the default workspace view.
"""

import logging

from pydantic import BaseModel, Field

from rumil.database import DB
from rumil.models import Call, MoveType, PageType
from rumil.moves.base import MoveDef, MoveResult

log = logging.getLogger(__name__)


class FinalizeArtefactPayload(BaseModel):
    note: str = Field(
        default="",
        description=(
            "Optional short note on why refinement ended here (e.g. 'converged', "
            "'budget exhausted', 'request under-specified'). Stored for audit only."
        ),
    )


async def execute(payload: FinalizeArtefactPayload, call: Call, db: DB) -> MoveResult:
    artefact_task_id = call.scope_page_id
    if not artefact_task_id:
        return MoveResult(
            message=(
                "ERROR: finalize_artefact requires call.scope_page_id set to "
                "the artefact-task question. No change made."
            ),
            created_page_id=None,
        )
    scope_page = await db.get_page(artefact_task_id)
    if scope_page is None or scope_page.page_type != PageType.QUESTION:
        actual = scope_page.page_type.value if scope_page else "missing"
        return MoveResult(
            message=(
                f"ERROR: finalize_artefact scope must be a question; got {actual}. No change made."
            ),
            created_page_id=None,
        )

    artefact = await db.latest_artefact_for_task(artefact_task_id)
    if artefact is None:
        return MoveResult(
            message=(
                f"ERROR: finalize_artefact found no artefact for task "
                f"{artefact_task_id[:8]}. Run generate_artefact first."
            ),
            created_page_id=None,
        )

    if not artefact.hidden:
        return MoveResult(
            message=f"Artefact [{artefact.id[:8]}] is already visible; nothing to do.",
            created_page_id=None,
        )

    await db.set_page_hidden(artefact.id, False)
    log.info(
        "Artefact finalized: %s (task=%s, note=%r)",
        artefact.id[:8],
        artefact_task_id[:8],
        payload.note,
    )
    return MoveResult(
        message=(f"Finalized artefact [{artefact.id[:8]}]: now visible to the workspace."),
        created_page_id=None,
        trace_extra={"artefact_id": artefact.id, "note": payload.note},
    )


MOVE = MoveDef(
    move_type=MoveType.FINALIZE_ARTEFACT,
    name="finalize_artefact",
    description=(
        "End the refinement loop and promote the latest artefact from hidden "
        "to visible. Use when further iteration is not worthwhile — because "
        "the artefact is good, the request won't converge further, or budget "
        "is better spent elsewhere. All iteration lineage (spec items, "
        "superseded drafts, critiques) stays hidden; only the final artefact "
        "surfaces to the workspace."
    ),
    schema=FinalizeArtefactPayload,
    execute=execute,
)
