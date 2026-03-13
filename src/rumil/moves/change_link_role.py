"""CHANGE_LINK_ROLE move: update a link's role between direct and structural."""

import logging

from pydantic import BaseModel, Field

from rumil.database import DB
from rumil.models import Call, LinkRole, MoveType
from rumil.moves.base import MoveDef, MoveResult

log = logging.getLogger(__name__)


class ChangeLinkRolePayload(BaseModel):
    link_id: str = Field(description="Full UUID of the link to update")
    new_role: LinkRole = Field(
        description=(
            "New role: 'direct' = this page directly bears on the answer; "
            "'structural' = this page frames what evidence/angles to explore."
        ),
    )
    reasoning: str = Field("", description="Why the role should change")


async def execute(
    payload: ChangeLinkRolePayload, call: Call, db: DB,
) -> MoveResult:
    trace_extra: dict = {}
    link = await db.get_link(payload.link_id)
    if link:
        trace_extra["old_role"] = link.role.value
        from_page = await db.get_page(link.from_page_id)
        to_page = await db.get_page(link.to_page_id)
        trace_extra["from_page"] = {
            "id": link.from_page_id,
            "summary": from_page.summary if from_page else "",
        }
        trace_extra["to_page"] = {
            "id": link.to_page_id,
            "summary": to_page.summary if to_page else "",
        }
    await db.update_link_role(payload.link_id, payload.new_role)
    log.info(
        "Link %s role changed to %s",
        payload.link_id[:8], payload.new_role.value,
    )
    return MoveResult(
        f"Link {payload.link_id[:8]} role changed to {payload.new_role.value}.",
        trace_extra=trace_extra,
    )


MOVE = MoveDef(
    move_type=MoveType.CHANGE_LINK_ROLE,
    name="change_link_role",
    description=(
        "Change a link's role between 'direct' and 'structural'. "
        "Direct links bear immediately on the answer; structural links "
        "frame the investigation."
    ),
    schema=ChangeLinkRolePayload,
    execute=execute,
)
