"""LOAD_PAGE move: request the full content of a page."""

import logging

from pydantic import BaseModel, Field

from differential.context import format_page
from differential.database import DB
from differential.models import Call, MoveType
from differential.moves.base import MoveDef, MoveResult

log = logging.getLogger(__name__)


class LoadPagePayload(BaseModel):
    page_id: str = Field(description="Short ID (first 8 chars) from the workspace map")


def execute(payload: LoadPagePayload, call: Call, db: DB) -> MoveResult:
    page_id = payload.page_id.strip()
    full_id = db.resolve_page_id(page_id)
    if not full_id:
        log.debug("load_page: page '%s' not found", page_id)
        return MoveResult(f"Page '{page_id}' not found.")
    page = db.get_page(full_id)
    if not page:
        log.debug("load_page: page '%s' resolved but not loadable", page_id)
        return MoveResult(f"Page '{page_id}' not found.")
    log.debug(
        "load_page: loaded %s (%s, %d chars)",
        full_id[:8], page.page_type.value, len(page.content),
    )
    return MoveResult(format_page(page, db=db))


MOVE = MoveDef(
    move_type=MoveType.LOAD_PAGE,
    name="load_page",
    description=(
        "Request the full content of a page by its short ID from the workspace "
        "map. The page content will be returned so you can read it before "
        "continuing your work."
    ),
    schema=LoadPagePayload,
    execute=execute,
)
