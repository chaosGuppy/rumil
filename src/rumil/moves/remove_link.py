"""REMOVE_LINK move: remove a link between pages."""

import logging

from pydantic import BaseModel, Field

from rumil.database import DB
from rumil.models import Call, MoveType
from rumil.moves.base import MoveDef, MoveResult

log = logging.getLogger(__name__)


class RemoveLinkPayload(BaseModel):
    link_id: str = Field(description="Full UUID of the link to remove")
    reasoning: str = Field("", description="Why this link should be removed")


async def execute(payload: RemoveLinkPayload, call: Call, db: DB) -> MoveResult:
    trace_extra: dict = {}
    link = await db.get_link(payload.link_id)
    if link:
        trace_extra["role"] = link.role.value
        from_page = await db.get_page(link.from_page_id)
        to_page = await db.get_page(link.to_page_id)
        trace_extra["from_page"] = {
            "id": link.from_page_id,
            "summary": from_page.headline if from_page else "",
        }
        trace_extra["to_page"] = {
            "id": link.to_page_id,
            "summary": to_page.headline if to_page else "",
        }
    await db.delete_link(payload.link_id)
    log.info("Link removed: %s", payload.link_id[:8])
    return MoveResult(
        f"Link {payload.link_id[:8]} removed.",
        trace_extra=trace_extra,
    )


MOVE = MoveDef(
    move_type=MoveType.REMOVE_LINK,
    name="remove_link",
    description="Remove a link between pages. Use when a link is no longer relevant.",
    schema=RemoveLinkPayload,
    execute=execute,
)
