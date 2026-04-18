"""FLAG_ISSUE move: flag a problem or suggested improvement about the call itself.

Unlike FLAG_FUNNINESS (which targets a specific page), this is meta-feedback:
something about the prompt, context, task framing, or available tools that the
human configuring the system should know about.
"""

import logging
from typing import Literal

from pydantic import BaseModel, Field

from rumil.database import DB
from rumil.models import Call, MoveType
from rumil.moves.base import MoveDef, MoveResult

log = logging.getLogger(__name__)


class FlagIssuePayload(BaseModel):
    category: Literal["problem", "improvement"] = Field(
        description=(
            "'problem' if something is wrong or confusing; 'improvement' if "
            "things work but you see a way they could work better."
        )
    )
    message: str = Field(
        description=(
            "What you want to flag. Be specific about what would help a human "
            "configuring the system — don't flag routine observations."
        )
    )
    suggested_fix: str = Field(
        default="",
        description="Optional: how this could be addressed.",
    )


async def execute(payload: FlagIssuePayload, call: Call, db: DB) -> MoveResult:
    note = f"[{payload.category}] {payload.message}"
    if payload.suggested_fix:
        note += f"\n\nSuggested fix: {payload.suggested_fix}"
    await db.save_page_flag("issue", call_id=call.id, note=note)
    log.info(
        "Issue flagged on call %s: category=%s, message=%s",
        call.id[:8],
        payload.category,
        payload.message[:80],
    )
    return MoveResult("Flag recorded. Continue your main task.")


MOVE = MoveDef(
    move_type=MoveType.FLAG_ISSUE,
    name="flag_issue",
    description=(
        "Flag a problem or improvement about this call's prompt, context, "
        "task framing, or available tools — meta-feedback for the human "
        "configuring the system. Only flag things that would meaningfully "
        "change how a human sets up or prompts calls like this one; don't "
        "flag routine observations. You may call this more than once if you "
        "have distinct issues, but keep each flag substantive."
    ),
    schema=FlagIssuePayload,
    execute=execute,
)
