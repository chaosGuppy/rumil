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
from rumil.moves.base import CreatePagePayload, MoveDef, MoveResult, create_page
from rumil.moves.link_consideration import ConsiderationLinkFields

log = logging.getLogger(__name__)


class CreateJudgementPayload(CreatePagePayload):
    key_dependencies: str | None = Field(
        None, description="What this judgement most depends on"
    )
    sensitivity_analysis: str | None = Field(
        None, description="What would shift this judgement, and in which direction"
    )
    links: list[ConsiderationLinkFields] = Field(
        default_factory=list,
        description=(
            "Question links to create for this judgement. Each entry "
            "links this judgement as a consideration bearing on an "
            "existing question, with a strength rating."
        ),
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
    if not result.created_page_id or not payload.links:
        return result

    for link_spec in payload.links:
        resolved = await db.resolve_page_id(link_spec.question_id)
        if not resolved:
            log.warning(
                "Inline judgement link skipped: question %s not found",
                link_spec.question_id,
            )
            continue

        await db.save_link(PageLink(
            from_page_id=result.created_page_id,
            to_page_id=resolved,
            link_type=LinkType.CONSIDERATION,
            strength=link_spec.strength,
            reasoning=link_spec.reasoning,
            role=link_spec.role,
        ))
        log.info(
            "Inline judgement linked: %s -> %s (%.1f)",
            result.created_page_id[:8], resolved[:8], link_spec.strength,
        )

    return result


MOVE = MoveDef(
    move_type=MoveType.CREATE_JUDGEMENT,
    name="create_judgement",
    description=(
        "Create a judgement — a considered position synthesising the "
        "considerations bearing on a question. Must engage with "
        "considerations on multiple sides. Include key_dependencies and "
        "sensitivity_analysis fields. Use the links field to simultaneously "
        "attach this judgement as a consideration on one or more questions."
    ),
    schema=CreateJudgementPayload,
    execute=execute,
)
