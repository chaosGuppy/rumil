"""FLAG_FUNNINESS move: flag something that seems off about a page."""

import logging

from pydantic import BaseModel, Field

from rumil.database import DB
from rumil.models import Call, MoveType
from rumil.moves.base import MoveDef, MoveResult

log = logging.getLogger(__name__)


class FlagFunninessPayload(BaseModel):
    page_id: str = Field(description="Page ID of the page that seems off")
    note: str = Field(description="What seems off")


async def execute(payload: FlagFunninessPayload, call: Call, db: DB) -> MoveResult:
    page_id = await db.resolve_page_id(payload.page_id)
    await db.save_page_flag("funniness", call_id=call.id, note=payload.note, page_id=page_id)
    if page_id is not None:
        await _record_flag_reputation(db, flagged_page_id=page_id, call_id=call.id)
    log.info("Funniness flagged: page=%s, note=%s", payload.page_id, payload.note[:80])
    return MoveResult("Done.")


async def _record_flag_reputation(db: DB, *, flagged_page_id: str, call_id: str) -> None:
    """Record a human-feedback reputation event tagged against the flagged page's run.

    Endogenous substrate hook (v1 reputation substrate). Each flag contributes
    one raw event with source='human_feedback', dimension='issue_flag',
    score=1.0. Never aggregate at write time — consumers can sum, average, or
    weight by source/dimension at query time. See
    marketplace-thread/07-feedback.md.
    """
    page = await db.get_page(flagged_page_id)
    subject_run_id = page.run_id if page is not None else ""
    orchestrator: str | None = None
    if subject_run_id:
        run_row = await db.get_run(subject_run_id)
        if run_row:
            config = run_row.get("config") or {}
            if isinstance(config, dict):
                val = config.get("orchestrator")
                orchestrator = val if isinstance(val, str) else None

    await db.record_reputation_event(
        source="human_feedback",
        dimension="issue_flag",
        score=1.0,
        orchestrator=orchestrator,
        source_call_id=call_id,
        extra={"subject_run_id": subject_run_id, "flagged_page_id": flagged_page_id},
    )


# TODO: additional endogenous sources not yet hooked (need their own substrates):
#   - source='proposal_survival' — when a view-edit proposal lives or dies
#   - source='budget_flow' — how much budget is directed toward a run
#   - source='subscription' — count of subscribers for a run
# See marketplace-thread/07-feedback.md.


MOVE = MoveDef(
    move_type=MoveType.FLAG_FUNNINESS,
    name="flag_funniness",
    description="Flag something that seems off or wrong about a page.",
    schema=FlagFunninessPayload,
    execute=execute,
)
