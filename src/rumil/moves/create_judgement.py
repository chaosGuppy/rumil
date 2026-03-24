"""CREATE_JUDGEMENT move: create a considered position on a question."""

import logging
from typing import Any

from pydantic import Field

from rumil.database import DB
from rumil.models import (
    Call,
    LinkType,
    MoveType,
    PageLayer,
    PageLink,
    PageType,
)
from rumil.moves.base import (
    CreatePagePayload,
    MoveDef,
    MoveResult,
    create_page,
    supersede_old_judgements,
)

log = logging.getLogger(__name__)


class CreateJudgementPayload(CreatePagePayload):
    key_dependencies: str | None = Field(
        None, description="What this judgement most depends on"
    )
    sensitivity_analysis: str | None = Field(
        None, description="What would shift this judgement, and in which direction"
    )

    def page_extra_fields(self) -> dict[str, Any]:
        extra = super().page_extra_fields()
        if self.key_dependencies is not None:
            extra["key_dependencies"] = self.key_dependencies
        if self.sensitivity_analysis is not None:
            extra["sensitivity_analysis"] = self.sensitivity_analysis
        return extra


async def execute(payload: CreateJudgementPayload, call: Call, db: DB) -> MoveResult:
    result = await create_page(payload, call, db, PageType.JUDGEMENT, PageLayer.SQUIDGY)
    if not result.created_page_id or not call.scope_page_id:
        return result

    await db.save_link(
        PageLink(
            from_page_id=result.created_page_id,
            to_page_id=call.scope_page_id,
            link_type=LinkType.RELATED,
        )
    )
    await supersede_old_judgements(result.created_page_id, call.scope_page_id, db)
    log.info(
        "Judgement %s auto-linked to scope question %s",
        result.created_page_id[:8],
        call.scope_page_id[:8],
    )

    return result


MOVE = MoveDef(
    move_type=MoveType.CREATE_JUDGEMENT,
    name="create_judgement",
    description=(
        "Create a judgement — a considered position synthesising the "
        "considerations bearing on a question. Must engage with "
        "considerations on multiple sides. Include key_dependencies and "
        "sensitivity_analysis fields. The judgement is automatically linked "
        "to the scope question and supersedes any prior judgement on it."
    ),
    schema=CreateJudgementPayload,
    execute=execute,
)
