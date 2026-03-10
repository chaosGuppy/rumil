"""FLAG_FUNNINESS move: flag something that seems off about a page."""

from pydantic import BaseModel, Field

from differential.database import DB
from differential.models import Call, MoveType
from differential.moves.base import MoveDef, MoveResult


class FlagFunninessPayload(BaseModel):
    page_id: str = Field(description="Page ID of the page that seems off")
    note: str = Field(description="What seems off")


def execute(payload: FlagFunninessPayload, call: Call, db: DB) -> MoveResult:
    page_id = db.resolve_page_id(payload.page_id)
    db.save_page_flag("funniness", call_id=call.id, note=payload.note, page_id=page_id)
    print(f"  [flag] Funniness flagged: {payload.note}")
    return MoveResult("Done.")


MOVE = MoveDef(
    move_type=MoveType.FLAG_FUNNINESS,
    name="flag_funniness",
    description="Flag something that seems off or wrong about a page.",
    schema=FlagFunninessPayload,
    execute=execute,
)
