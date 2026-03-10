"""SUPERSEDE_PAGE move: replace an existing page with an improved version."""

import logging

from pydantic import Field

from differential.database import DB
from differential.models import Call, MoveType
from differential.moves.base import CreatePagePayload, MoveDef, MoveResult, create_page

log = logging.getLogger(__name__)


class SupersedePagePayload(CreatePagePayload):
    old_page_id: str = Field(description="Page ID of the page to replace")


async def execute(payload: SupersedePagePayload, call: Call, db: DB) -> MoveResult:
    old_id = await db.resolve_page_id(payload.old_page_id)
    if not old_id:
        log.warning("SUPERSEDE_PAGE: page %s not found", payload.old_page_id)
        return MoveResult(f"Supersede skipped — page {payload.old_page_id} not found.")

    old_page = await db.get_page(old_id)
    if not old_page:
        log.warning("SUPERSEDE_PAGE: page %s resolved but not loadable", old_id[:8])
        return MoveResult(f"Supersede skipped — page {old_id} not found.")

    result = await create_page(payload, call, db, old_page.page_type, old_page.layer)
    await db.supersede_page(old_id, result.created_page_id)
    log.info("Superseded %s -> %s", old_id[:8], result.created_page_id[:8])
    return result


MOVE = MoveDef(
    move_type=MoveType.SUPERSEDE_PAGE,
    name="supersede_page",
    description=(
        "Replace an existing page with an improved version. The old page is "
        "marked as superseded and the new page links back to it."
    ),
    schema=SupersedePagePayload,
    execute=execute,
)
