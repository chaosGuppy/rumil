"""SUPERSEDE_PAGE move: replace an existing page with an improved version."""

from pydantic import Field

from differential.database import DB
from differential.models import Call, MoveType
from differential.moves.base import CreatePagePayload, MoveDef, MoveResult, create_page


class SupersedePagePayload(CreatePagePayload):
    old_page_id: str = Field(description="Page ID of the page to replace")


def execute(payload: SupersedePagePayload, call: Call, db: DB) -> MoveResult:
    old_id = db.resolve_page_id(payload.old_page_id)
    if not old_id:
        print(f"  [executor] SUPERSEDE_PAGE: page {payload.old_page_id} not found")
        return MoveResult(f"Supersede skipped — page {payload.old_page_id} not found.")

    old_page = db.get_page(old_id)
    if not old_page:
        print(f"  [executor] SUPERSEDE_PAGE: page {old_id} not found")
        return MoveResult(f"Supersede skipped — page {old_id} not found.")

    result = create_page(payload, call, db, old_page.page_type, old_page.layer)
    db.supersede_page(old_id, result.created_page_id)
    print(
        f"  [~] Superseded {db.page_label(old_id)} -> "
        f"{db.page_label(result.created_page_id)}"
    )
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
