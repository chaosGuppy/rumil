"""REPORT_DUPLICATE move: flag two pages as duplicates."""

import logging

from pydantic import BaseModel, Field

from differential.models import MoveType
from differential.moves.base import MoveDef, MoveResult, MoveState

log = logging.getLogger(__name__)


class ReportDuplicatePayload(BaseModel):
    page_id_a: str = Field(description="Page ID of the first duplicate")
    page_id_b: str = Field(description="Page ID of the second duplicate")


async def execute(payload: ReportDuplicatePayload, state: MoveState) -> MoveResult:
    db = state.db
    pid_a = await db.resolve_page_id(payload.page_id_a)
    pid_b = await db.resolve_page_id(payload.page_id_b)
    await db.save_page_flag("duplicate", call_id=state.call.id, page_id_a=pid_a, page_id_b=pid_b)
    log.info(
        "Duplicate reported: %s <-> %s", payload.page_id_a, payload.page_id_b,
    )
    return MoveResult("Done.")


MOVE = MoveDef(
    move_type=MoveType.REPORT_DUPLICATE,
    name="report_duplicate",
    description="Flag two pages as duplicates of each other.",
    schema=ReportDuplicatePayload,
    execute=execute,
)
