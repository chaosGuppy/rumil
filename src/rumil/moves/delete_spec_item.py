"""DELETE_SPEC_ITEM: remove a spec item from the current spec.

Implemented by deleting the SPEC_OF link from the spec item to the
artefact-task question. The spec-item page itself is left as an orphan so
historical artefact snapshots (via GENERATED_FROM links) still render past
specs correctly even after the refiner decides an item never should have
been there.
"""

import logging

from pydantic import BaseModel, Field

from rumil.database import DB
from rumil.models import Call, LinkType, MoveType, PageType
from rumil.moves.base import MoveDef, MoveResult

log = logging.getLogger(__name__)


class DeleteSpecItemPayload(BaseModel):
    spec_id: str = Field(
        description=(
            "Full or short ID of the spec item to remove. The spec item is "
            "dropped from the current spec; past artefact snapshots that "
            "were generated with it remain intact."
        ),
    )


async def execute(payload: DeleteSpecItemPayload, call: Call, db: DB) -> MoveResult:
    artefact_task_id = call.scope_page_id
    if not artefact_task_id:
        return MoveResult(
            message=(
                "ERROR: delete_spec_item requires call.scope_page_id set to "
                "the artefact-task question. No change made."
            ),
            created_page_id=None,
        )
    scope_page = await db.get_page(artefact_task_id)
    if scope_page is None or scope_page.page_type != PageType.QUESTION:
        actual = scope_page.page_type.value if scope_page else "missing"
        return MoveResult(
            message=(
                f"ERROR: delete_spec_item scope must be a question; got {actual}. No change made."
            ),
            created_page_id=None,
        )

    spec_id = await db.resolve_page_id(payload.spec_id)
    if not spec_id:
        return MoveResult(
            message=f"ERROR: delete_spec_item could not resolve spec_id={payload.spec_id!r}.",
            created_page_id=None,
        )
    spec_page = await db.get_page(spec_id)
    if spec_page is None or spec_page.page_type != PageType.SPEC_ITEM:
        actual = spec_page.page_type.value if spec_page else "missing"
        return MoveResult(
            message=(
                f"ERROR: delete_spec_item target {payload.spec_id!r} is not a "
                f"spec item (got {actual}). No change made."
            ),
            created_page_id=None,
        )

    outgoing = await db.get_links_from(spec_id)
    spec_of_links = [
        l for l in outgoing if l.link_type == LinkType.SPEC_OF and l.to_page_id == artefact_task_id
    ]
    if not spec_of_links:
        return MoveResult(
            message=(
                f"Spec item [{spec_id[:8]}] is already not part of this spec — nothing to delete."
            ),
            created_page_id=None,
        )
    for link in spec_of_links:
        await db.delete_link(link.id)

    log.info(
        "Spec item deleted from current spec: %s (task=%s)",
        spec_id[:8],
        artefact_task_id[:8],
    )
    return MoveResult(
        message=f"Deleted spec item [{spec_id[:8]}] from the current spec.",
        created_page_id=None,
    )


MOVE = MoveDef(
    move_type=MoveType.DELETE_SPEC_ITEM,
    name="delete_spec_item",
    description=(
        "Remove a spec item from the current spec with no replacement. Use "
        "when a rule was simply wrong and should be dropped entirely — for "
        "replacing it with a revised rule, use supersede_spec_item. The "
        "spec-item page remains in the workspace (unlinked) so past artefact "
        "snapshots that were generated with it still render faithfully."
    ),
    schema=DeleteSpecItemPayload,
    execute=execute,
)
