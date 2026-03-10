"""LOAD_PAGE move: request the full content of a page."""

from pydantic import BaseModel, Field

from differential.context import format_page
from differential.database import DB
from differential.models import Call, MoveType
from differential.moves.base import MoveDef, MoveResult


class LoadPagePayload(BaseModel):
    page_id: str = Field(description="Short ID (first 8 chars) from the workspace map")


def execute(payload: LoadPagePayload, call: Call, db: DB) -> MoveResult:
    page_id = payload.page_id.strip()
    full_id = db.resolve_page_id(page_id)
    if not full_id:
        return MoveResult(f"Page '{page_id}' not found.")
    page = db.get_page(full_id)
    if not page:
        return MoveResult(f"Page '{page_id}' not found.")
    print(f"  [load] {db.page_label(full_id)}")
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
