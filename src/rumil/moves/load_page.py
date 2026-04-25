"""LOAD_PAGE move: request the full content of a page."""

import logging

from pydantic import BaseModel, Field

from rumil.context import format_page
from rumil.database import DB
from rumil.models import Call, MoveType, PageDetail
from rumil.moves.base import MoveDef, MoveResult

log = logging.getLogger(__name__)


_DETAIL_MAP: dict[str, PageDetail] = {
    "abstract": PageDetail.ABSTRACT,
    "content": PageDetail.CONTENT,
}


class LoadPagePayload(BaseModel):
    page_id: str = Field(description="Short ID (first 8 chars) from the workspace map")
    detail: str = Field(
        default="content",
        description=(
            "Level of detail: 'content' (full text, default) or 'abstract' (short summary)"
        ),
    )


async def execute(payload: LoadPagePayload, call: Call, db: DB) -> MoveResult:
    page_id = payload.page_id.strip()
    full_id = await db.resolve_page_id(page_id)
    if not full_id:
        log.debug("load_page: page '%s' not found", page_id)
        return MoveResult(f"Page '{page_id}' not found.")
    page = await db.get_page(full_id)
    if not page:
        log.debug("load_page: page '%s' resolved but not loadable", page_id)
        return MoveResult(f"Page '{page_id}' not found.")
    detail = _DETAIL_MAP.get(payload.detail, PageDetail.CONTENT)
    log.debug(
        "load_page: loaded %s (%s, detail=%s, %d chars)",
        full_id[:8],
        page.page_type.value,
        detail.value,
        len(page.content),
    )
    return MoveResult(
        await format_page(
            page,
            detail,
            db=db,
            track=True,
            track_tags={"source": "load_page_move"},
        )
    )


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
